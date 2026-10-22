"""Non-diagnostic health scheduling; tick never reads a database or runs analytics.

One daemon-owned instance, one background worker at most. Call close on shutdown.
Critical means enqueue urgency=high immediately after the 30-minute poll; actual
channel dispatch remains Outreach's responsibility (this is not emergency care).
All source health/mind queries are read-only. dry_run returns counts/keys only.

State is a private, fsync+replace journal, serialized with a per-root file lock.
Stable origins recover graph/report writes after restart. A real Outreach queue
is read back by dedupe_key to recover enqueue-before-state-save crashes. Custom
transports must implement equivalent durable dedupe for exactly-once handoff.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import sqlite3
import stat
import tempfile
import threading
from collections.abc import Callable, Mapping
from contextlib import closing, contextmanager
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, TypeGuard, cast
from zoneinfo import ZoneInfo

import product_home

CST = ZoneInfo("Asia/Shanghai")
METRICS = ("heart_rate", "sleep", "steps", "spo2", "temperature")
LABELS = {
    "heart_rate": "心率",
    "sleep": "睡眠",
    "steps": "活动",
    "spo2": "血氧",
    "temperature": "温度",
}
PERIOD = timedelta(minutes=30)
DEDUPE = timedelta(hours=24)
Record = dict[str, Any]


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(CST) if result.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _numeric(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _record(value: Any) -> Record:
    if isinstance(value, Mapping):
        return dict(cast(Mapping[str, Any], value))
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return dict(vars(value))


def _identity(alert: Record) -> str:
    # A rule, not its fluctuating reading or freshly generated timestamp, is the
    # alert identity. Opposing high/low rules on one metric remain distinct.
    context = alert.get("context")
    try:
        details = json.loads(context) if isinstance(context, str) else context
    except (ValueError, TypeError):
        details = None
    rule = cast(Record, details).get("rule_id") if isinstance(details, dict) else None
    key = [
        alert.get("severity"),
        alert.get("metric"),
        rule or alert.get("message"),
        alert.get("threshold"),
    ]
    return hashlib.sha256(json.dumps(key, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class HealthScheduler:
    """Inject analyzer/rules/manager/outreach/MindStore and an aware clock callable.

    Clock objects exposing now() are also accepted. wait() is for tests/shutdown,
    never for the daemon loop. No thread, notification or read starts on import.
    """

    def __init__(
        self,
        root: Path,
        analyzer: Any,
        rules: Any,
        alert_manager: Any,
        outreach: Any,
        mind_store: Any,
        clock: Callable[[], datetime] | Any,
    ):
        self.root = product_home.validate_root(Path(root))
        self.analyzer = analyzer
        self.rules = rules
        self.alert_manager = alert_manager
        self.outreach = outreach
        self.mind_store = mind_store
        self.clock = clock
        self.state_path = self.root / "health_scheduler_state.json"
        self.lock_path = self.root / ".health_scheduler.lock"
        self._guard = threading.Lock()
        self._worker: threading.Thread | None = None
        self._next_due: datetime | None = None
        self._closed = False
        self._last_error_type: str | None = None
        self._last_counts: Record = {}

    def _now(self) -> datetime:
        now = self.clock() if callable(self.clock) else self.clock.now()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now.astimezone(CST)

    def tick(self) -> bool:
        """Schedule due work and return immediately; False means idle/already busy."""
        now = self._now()
        with self._guard:
            if self._closed or (self._worker is not None and self._worker.is_alive()):
                return False
            if self._next_due is not None and now < self._next_due:
                return False
            self._worker = threading.Thread(
                target=self._run, args=(now,), name="health-scheduler", daemon=True
            )
            self._worker.start()
            return True

    def wait(self, timeout: float | None = None) -> bool:
        with self._guard:
            worker = self._worker
        if worker is not None:
            worker.join(timeout)
        return worker is None or not worker.is_alive()

    def close(self, timeout: float | None = None) -> bool:
        with self._guard:
            self._closed = True
        return self.wait(timeout)

    @staticmethod
    def _private_file(path: Path) -> None:
        product_home.safe_absolute(path)
        if path.exists():
            info = path.stat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ValueError("Unsafe scheduler file")

    @contextmanager
    def _locked(self):
        self._private_file(self.lock_path)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load(self) -> Record:
        self._private_file(self.state_path)
        if not self.state_path.exists():
            return {
                "version": 1,
                "critical": {},
                "projections": {},
                "last_check_at": None,
                "last_report_date": None,
            }
        raw_state: Any = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(raw_state, dict):
            raise ValueError("Invalid scheduler state")
        state = cast(Record, raw_state)
        if (
            state.get("version") != 1
            or not isinstance(state.get("critical"), dict)
            or not isinstance(state.get("projections"), dict)
        ):
            raise ValueError("Invalid scheduler state; refusing to erase dedupe history")
        return state

    def _save(self, state: Record) -> None:
        self._private_file(self.state_path)
        fd, name = tempfile.mkstemp(prefix=".health-scheduler-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, allow_nan=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.state_path)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _enqueue(self, text: str, key: str, urgency: str, now: datetime) -> str:
        # Outreach's own six-hour rolling dedupe is shorter than this scheduler's
        # 24h journal. Exact intent read-back closes an enqueue/save crash window.
        path = getattr(self.outreach, "db_path", None)
        if path is not None and Path(path).is_file():
            with closing(
                sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True)
            ) as conn:
                row = conn.execute(
                    "SELECT id FROM messages WHERE dedupe_key=? ORDER BY rowid LIMIT 1", (key,)
                ).fetchone()
                if row:
                    return str(row[0])
        return self.outreach.enqueue(
            text,
            reason="健康哨兵" if urgency == "high" else "每日健康晨报",
            urgency=urgency,
            channel="auto",
            dedupe_key=key,
            studying=False,
            now=now,
        )

    @staticmethod
    def _letter(body: str) -> str:
        return "Sir：\n\n" + body[:300] + "\n\nJARVIS"

    def _alerts(self, summary: Record) -> list[Record]:
        return [_record(item) for item in self.rules.evaluate(summary)]

    def _project_alerts(self, alerts: list[Record], target: str, state: Record) -> None:
        pending = state.setdefault("pending_projections", {})
        for alert in alerts:
            identity = f"health:{target}:{_identity(alert)}"
            if identity not in state["projections"]:
                pending[identity] = {"alert": {**alert, "id": identity}, "date": target}
        if pending:
            self._save(state)
        first_error = None
        for identity, record in list(pending.items()):
            try:
                self.alert_manager.process_alerts([record["alert"]])
                state["projections"][identity] = record["date"]
                del pending[identity]
                self._save(state)
            except Exception as error:
                first_error = first_error or error
        if first_error is not None:
            raise first_error

    def _send_pending_critical(self, now: datetime, state: Record) -> int:
        sent_count = 0
        first_error = None
        for fingerprint, record in list(state["critical"].items()):
            if not record.get("pending_key"):
                continue
            try:
                message_id = self._enqueue(record["text"], record["pending_key"], "high", now)
                state["critical"][fingerprint] = {
                    "sent_at": now.isoformat(),
                    "message_id": message_id,
                }
                self._save(state)
                sent_count += 1
            except Exception as error:
                first_error = first_error or error
        if first_error is not None:
            raise first_error
        return sent_count

    def _sentinel(self, now: datetime, state: Record) -> Record:
        count = critical = 0
        batches: list[tuple[str, list[Record]]] = []
        # Today is required: analyzer's default is yesterday, which would miss a
        # newly-arrived critical reading. Yesterday retains sleep/streak evidence.
        for target in (now.date(), now.date() - timedelta(days=1)):
            alerts = self._alerts(self.analyzer.daily_summary(target.isoformat()))
            count += len(alerts)
            batches.append((target.isoformat(), alerts))
            for alert in alerts:
                if alert.get("severity") != "critical":
                    continue
                fingerprint = _identity(alert)
                record = state["critical"].get(fingerprint, {})
                sent = _time(record.get("sent_at"))
                if sent is not None and now < sent + DEDUPE:
                    continue
                if not record.get("pending_key"):
                    state["critical"][fingerprint] = {
                        "pending_key": f"health-critical:{fingerprint}:{now.isoformat()}",
                        "text": self._letter(str(alert["message"])),
                    }
        # Persist both routes before touching either dependency. A graph outage
        # must not suppress critical handoff; an outreach outage must not lose KG.
        pending = state.setdefault("pending_projections", {})
        for target, alerts in batches:
            for alert in alerts:
                identity = f"health:{target}:{_identity(alert)}"
                if identity not in state["projections"]:
                    pending[identity] = {"alert": {**alert, "id": identity}, "date": target}
        self._save(state)
        first_error = None
        try:
            critical = self._send_pending_critical(now, state)
        except Exception as error:
            first_error = error
        try:
            self._project_alerts([], "", state)
        except Exception as error:
            first_error = first_error or error
        if first_error is not None:
            raise first_error
        state["last_check_at"] = now.isoformat()
        # Bound projection bookkeeping without weakening the 24-hour dedupe.
        cutoff = (now.date() - timedelta(days=30)).isoformat()
        state["projections"] = {k: v for k, v in state["projections"].items() if v >= cutoff}
        self._save(state)
        return {"alert_count": count, "critical_enqueued": critical}

    def _context(self, target: date) -> Record:
        start = datetime.combine(target - timedelta(days=1), datetime.min.time(), CST).replace(
            hour=18
        )
        stop = datetime.combine(target, datetime.min.time(), CST).replace(hour=8)
        result: Record = {
            "window_start": start.isoformat(),
            "window_end": stop.isoformat(),
            "last_computer_activity_at": None,
            "late_computer_activity": None,
            "computer_event_count": 0,
            "browser_visit_count": 0,
            "browser_content_status": "missing",
            "evidence_ids": [],
            "sleep_inference": "computer_activity_is_not_sleep_or_attention",
        }
        path = Path(self.mind_store.path).absolute()
        # Canonical MindStore wraps sense_event under payload.data; two legacy
        # direct shapes are also supported. No title/URL/domain leaves this reader.
        event = (
            "COALESCE(json_extract(payload,'$.data.sense_event'),"
            "json_extract(payload,'$.sense_event'),payload)"
        )
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
            conn.execute("PRAGMA query_only=ON")
            query = f"""SELECT id,{event} AS sensor FROM entries
                WHERE category IN ('event','activitywatch','browser_history')
                AND json_valid(payload)
                AND json_extract({event},'$.source') IN ('activitywatch','browser_history')
                AND julianday(COALESCE(json_extract({event},'$.observed_at'),
                    json_extract({event},'$.payload.sampled_at'))) >= julianday(?)
                AND julianday(COALESCE(json_extract({event},'$.observed_at'),
                    json_extract({event},'$.payload.sampled_at'))) < julianday(?)"""
            for row in conn.execute(query, (start.isoformat(), stop.isoformat())):
                sensor = json.loads(row[1])
                raw_data: Any = sensor.get("payload") or {}
                if not isinstance(raw_data, dict):
                    continue
                data = cast(Record, raw_data)
                if any(
                    container.get(flag) is True
                    for container in (sensor, data)
                    for flag in ("synthetic", "backfill", "background_only")
                ):
                    continue
                if data.get("status") in {"missing", "unavailable", "unknown"}:
                    continue
                observed = _time(sensor.get("observed_at") or data.get("sampled_at"))
                if observed is None:
                    continue
                source, kind = sensor.get("source"), sensor.get("kind")
                if source == "activitywatch" and kind == "app_usage":
                    duration = data.get("duration_seconds")
                    end = _time(data.get("end"))
                    if end is None and _numeric(duration) and 0 <= duration <= 86400:
                        end = observed + timedelta(seconds=float(duration))
                    if end is None or end < observed or end >= stop:
                        continue  # a session crossing 08:00 is not a last-night endpoint
                    result["computer_event_count"] += 1
                    previous = _time(result["last_computer_activity_at"])
                    if previous is None or end > previous:
                        result["last_computer_activity_at"] = end.isoformat()
                    result["evidence_ids"].append(row[0])
                elif source == "browser_history" and kind == "page_visit":
                    result["browser_visit_count"] += 1
                    # Presence is not enough to infer what was read or for how long.
                    if isinstance(data.get("title"), str) or isinstance(data.get("url"), str):
                        result["browser_content_status"] = "available_not_reproduced"
                    result["evidence_ids"].append(row[0])
        last = _time(result["last_computer_activity_at"])
        if last is not None:
            result["late_computer_activity"] = last >= start.replace(hour=23)
        return result

    def _measurements(self, target: date) -> Record:
        """Optional HRV / RHR timestamp evidence absent from analyzer's interface.

        Both known health DB schemas are read-only. Unsupported/missing data is
        explicitly missing; no RMSSD, baseline, diagnosis or fabricated timestamp.
        """
        result: Record = {
            key: {"status": "missing", "sample_count": 0, "mean": None, "latest_at": None}
            for key in ("resting_heart_rate", "hrv")
        }
        path = getattr(self.analyzer, "health_db_path", None)
        if path is None or not Path(path).is_file():
            return result
        with closing(
            sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True, timeout=5)
        ) as conn:
            conn.execute("PRAGMA query_only=ON")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(points)")}
            aliases = {
                "resting_heart_rate": (
                    "resting_heart_rate",
                    "HKQuantityTypeIdentifierRestingHeartRate",
                ),
                "hrv": (
                    "heart_rate_variability",
                    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN",
                    "hrv",
                ),
            }
            for metric, names in aliases.items():
                marks = ",".join("?" for _ in names)
                if {"metric", "ts", "day", "qty"} <= columns:
                    rows = conn.execute(
                        f"SELECT ts,qty FROM points WHERE metric IN ({marks}) AND day=?",
                        (*names, target.isoformat()),
                    )
                elif {"type", "startDate", "value"} <= columns:
                    low = (target - timedelta(days=1)).isoformat()
                    high = (target + timedelta(days=2)).isoformat()
                    rows = conn.execute(
                        f"SELECT startDate,value FROM points WHERE type IN ({marks}) "
                        "AND startDate>=? AND startDate<?",
                        (*names, low, high),
                    )
                else:
                    continue
                valid: list[tuple[datetime, float]] = []
                for raw_time, value in rows:
                    stamp = _time(raw_time)
                    try:
                        number = float(value) if not isinstance(value, bool) else float("nan")
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if (
                        stamp is not None
                        and stamp.date() == target
                        and math.isfinite(number)
                        and number > 0
                    ):
                        valid.append((stamp, number))
                if valid:
                    result[metric] = {
                        "status": "observed",
                        "sample_count": len(valid),
                        "mean": sum(x[1] for x in valid) / len(valid),
                        "latest_at": max(x[0] for x in valid).isoformat(),
                        "basis": "exported_samples_not_rmssd"
                        if metric == "hrv"
                        else "exported_samples",
                    }
        return result

    @staticmethod
    def _metric_status(summary: Record, alerts: list[Record]) -> Record:
        fields = {
            "heart_rate": ("avg_resting_heart_rate", "avg_heart_rate"),
            "sleep": ("avg_sleep_hours",),
            "steps": ("avg_daily_steps",),
            "spo2": ("avg_spo2",),
            "temperature": ("avg_temperature",),
        }
        result: Record = {}
        for key in METRICS:
            section = summary.get(key, {})
            present = any(_numeric(section.get(field)) for field in fields[key])
            result[key] = {
                "status": "flagged"
                if any(a.get("metric") == key for a in alerts)
                else "observed"
                if present
                else "missing",
                "analysis": section,
            }
        return result

    def _build_report(self, target: date) -> tuple[Record, list[Record]]:
        summary = self.analyzer.daily_summary(target.isoformat())
        alerts = self._alerts(summary)
        context = self._context(target)
        measurements = self._measurements(target)
        metrics = self._metric_status(summary, alerts)
        status_labels = {"flagged": "需留意", "observed": "已记录", "missing": "缺测"}
        overview = (
            "；".join(LABELS[k] + status_labels[metrics[k]["status"]] for k in METRICS) + "。"
        )
        # The existing report builder supplies advice; add five-class coverage and
        # observational context, never equating missing data with healthy values.
        advice = self.alert_manager.generate_health_report(summary, alerts)
        advice = str(advice).removeprefix("Sir，").removeprefix("Sir，你当前的健康概况：")
        if all(m["status"] == "missing" for m in metrics.values()):
            advice = "数据不足，暂不判断健康状态；请检查设备佩戴与同步，再复核趋势。"
        last = _time(context["last_computer_activity_at"])
        night = (
            "前夜电脑活动最后见于" + last.strftime("%H:%M") + "，不等于入睡时间。"
            if last
            else "前夜电脑活动缺测，无法推断作息。"
        )
        browser = (
            "有夜间浏览记录，内容未复述，不据此推断阅读或注意力。"
            if context["browser_visit_count"]
            else "浏览上下文缺测。"
        )
        hrv = (
            "HRV缺测，不作关联判断。"
            if measurements["hrv"]["status"] == "missing"
            else "HRV关联仅作时序候选，不能认定因果。"
        )
        # Reserve the recommendation at the end even when upstream advice is long.
        recommendation = "今天优先规律休息，异常先复测；若不适或持续异常请及时就医。"
        prefix = overview + night + browser + hrv
        body = prefix + advice[: max(0, 300 - len(prefix) - len(recommendation))] + recommendation
        return (
            {
                "date": target.isoformat(),
                "timezone": "Asia/Shanghai",
                "body": body,
                "text": self._letter(body),
                "metrics": metrics,
                "context": context,
                "measurements": measurements,
                "non_diagnostic": True,
            },
            alerts,
        )

    def _existing_report(self, target: str) -> Record | None:
        path = Path(self.mind_store.path).absolute()
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
            row = conn.execute(
                "SELECT id,payload FROM entries WHERE category='health_daily_report' "
                "AND json_extract(payload,'$.date')=? ORDER BY seq LIMIT 1",
                (target,),
            ).fetchone()
            return {"id": row[0], "payload": json.loads(row[1])} if row else None

    def _report_graph(self, report: Record, entry_id: str, now: datetime) -> None:
        target = report["date"]
        # Use manager projection for the report just as for alerts. Its graph is
        # also the sole graph handle used for the explicitly noncausal candidates.
        self.alert_manager.process_alerts(
            [
                {
                    "id": f"health-daily-report:{target}",
                    "severity": "info",
                    "metric": "health_daily_report",
                    "message": report["body"],
                    "source": "health_scheduler",
                    "created_at": now.isoformat(),
                    "evidence_ids": [entry_id],
                }
            ]
        )
        context = report["context"]
        last = _time(context["last_computer_activity_at"])
        if not context["late_computer_activity"] or last is None:
            return
        graph = self.alert_manager.knowledge_graph
        context_refs = context["evidence_ids"]
        refs = [entry_id, *context_refs]
        behavior = graph.add_node(
            "behavior_pattern",
            "health",
            "late_computer_activity",
            "晚间电脑活动；并非已证实晚睡",
            0.5,
            "health_scheduler",
            context_refs,
            origin_table="health_scheduler_context",
            origin_id=target,
            metadata={
                "event_time": last.isoformat(),
                "candidate_only": True,
                "sleep_not_inferred": True,
            },
        )
        for metric, reading in report["measurements"].items():
            measured = _time(reading["latest_at"])
            if measured is None or measured <= last:
                continue
            node = graph.add_node(
                "health_event",
                "health",
                f"next_day_{metric}",
                "次日观测；时序候选，不是异常或因果结论",
                0.5,
                "health_scheduler",
                [f"health_point:{metric}:{measured.isoformat()}"],
                origin_table="health_scheduler_measurements",
                origin_id=f"{target}:{metric}",
                metadata={
                    "event_time": measured.isoformat(),
                    "candidate_only": True,
                    "measurement_date": target,
                    "sample_count": reading["sample_count"],
                },
            )
            graph.add_edge(
                behavior,
                node,
                "precedes",
                0.2,
                refs,
                "晚间电脑活动先于次日静息心率/HRV观测；晚睡关联待验证，不能证明因果。",
            )

    def _morning(self, now: datetime, state: Record) -> None:
        target = (now.date() - timedelta(days=1)).isoformat()
        if now.hour < 8 or state.get("last_report_date") == target:
            return
        existing = self._existing_report(target)
        if existing:
            report, entry_id = existing["payload"], existing["id"]
        else:
            report, alerts = self._build_report(date.fromisoformat(target))
            self._project_alerts(alerts, target, state)
            entry_id = self.mind_store.append(
                "health_daily_report",
                report,
                source="health_scheduler",
                refs=report["context"]["evidence_ids"],
            )["id"]
        self._report_graph(report, entry_id, now)
        self._enqueue(report["text"], f"health-daily-report:{target}", "normal", now)
        state["last_report_date"] = target
        self._save(state)

    def _run(self, now: datetime) -> None:
        try:
            with self._locked():
                state = self._load()
                last = _time(state.get("last_check_at"))
                counts: Record = {}
                first_error = None
                try:
                    self._send_pending_critical(now, state)
                    if last is None or now >= last + PERIOD or now < last:
                        counts = self._sentinel(now, state)
                except Exception as error:
                    first_error = error
                try:
                    self._morning(now, state)
                except Exception as error:
                    first_error = first_error or error
                if first_error is not None:
                    raise first_error
                last = _time(state.get("last_check_at")) or now
                next_morning = now.replace(hour=8, minute=0, second=0, microsecond=0)
                if now >= next_morning:
                    next_morning += timedelta(days=1)
                with self._guard:
                    self._next_due = min(last + PERIOD, next_morning)
                    self._last_counts = counts
                    self._last_error_type = None
        except Exception as error:
            # Type only. Exception messages, tracebacks, reports and readings can
            # contain private health information and must never reach logs.
            with self._guard:
                self._last_error_type = type(error).__name__
                self._next_due = now + timedelta(minutes=1)

    def dry_run(self) -> Record:
        """Read real sources without writing state, mind, graph or sending anything.

        This synchronous, explicit diagnostic is not called by tick. Returned
        values are only counts and schema/status keys, never health readings.
        """
        now = self._now()
        target = now.date() - timedelta(days=1)
        report, alerts = self._build_report(target)
        today_alerts = self._alerts(self.analyzer.daily_summary(now.date().isoformat()))
        all_alerts = alerts + today_alerts
        candidate_count = 0
        last = _time(report["context"]["last_computer_activity_at"])
        if report["context"]["late_computer_activity"] and last is not None:
            for reading in report["measurements"].values():
                measured = _time(reading["latest_at"])
                if measured is not None and measured > last:
                    candidate_count += 1
        return {
            "dry_run": True,
            "messages_sent": 0,
            "writes": 0,
            "metric_keys": list(report["metrics"]),
            "metric_status_counts": {
                status: sum(m["status"] == status for m in report["metrics"].values())
                for status in ("missing", "observed", "flagged")
            },
            "alert_count": len(all_alerts),
            "critical_count": sum(a.get("severity") == "critical" for a in all_alerts),
            "computer_event_count": report["context"]["computer_event_count"],
            "browser_visit_count": report["context"]["browser_visit_count"],
            "context_keys": sorted(report["context"]),
            "measurement_keys": sorted(report["measurements"]),
            "report_body_char_count": len(report["body"]),
            "candidate_edge_count": candidate_count,
        }

    def self_evaluate(self) -> Record:
        with self._guard:
            return {
                "module": "health_scheduler",
                "domain": "health scheduling and noncausal context fusion",
                "current_version": "1",
                "timezone": "Asia/Shanghai",
                "worker_running": self._worker is not None and self._worker.is_alive(),
                "closed": self._closed,
                "last_error_type": self._last_error_type,
                "last_counts": dict(self._last_counts),
                "known_limitations": [
                    "Thirty-minute checks, not continuous/emergency monitoring.",
                    "High handoff obeys Outreach quiet-time/channel policy.",
                    "Foreground activity is not sleep or attention; causality is not inferred.",
                    "Upstream unknown browser records cannot be recovered from mind.db.",
                    "Browser content is not reproduced/classified; missing remains missing.",
                    "Custom outreach needs dedupe; manager mind appends can repeat on crashes.",
                    "Report wording uses HealthAlertManager, not model personality rendering.",
                ],
                "improvement_directions": [
                    "Model-authored report rendering behind a bounded validated interface.",
                    "Validated longitudinal sleep/HRV association analysis.",
                ],
            }
