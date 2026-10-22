"""Local thinking policy. This standalone module is not wired into the daemon.

No model calls, network calls, red-team execution or user-facing prose is produced
by routing. Registry IDs are recommendations, not runnable provider configurations.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
import uuid
from collections.abc import Callable, Generator
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from model_registry import ModelRegistry
from task_executor import classify_task, select_model

if TYPE_CHECKING:
    from inner_loop import MindStore
    from knowledge_graph import KnowledgeGraph
    from sir_profile import SirProfile

CST = timezone(timedelta(hours=8), name="CST")
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
EXTREME = frozenset(EFFORTS[3:])
DEFAULT_WEIGHTS = {
    "length": 0.15,
    "density": 0.15,
    "emotion": 0.1,
    "inference": 0.35,
    "multistep": 0.25,
}


@dataclass
class ThinkDecision:
    layer: str
    bypass_model: bool = False
    answer: str | None = None
    effort: str = "low"
    model_id: str | None = None
    needs_critic: bool = False
    conflict: str | None = None
    queued: bool = False
    log_entry: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())


class ThinkController:
    """Ten-stage local routing; completion/cache/feedback are explicit caller hooks."""

    def __init__(
        self,
        data_root: Path,
        mind_store: MindStore | None,
        sir_profile: SirProfile | None = None,
        knowledge_graph: KnowledgeGraph | None = None,
    ):
        self.data_root = Path(data_root).expanduser().absolute()
        if ".." in self.data_root.parts or any(
            p.is_symlink() for p in (self.data_root, *self.data_root.parents)
        ):
            raise ValueError("Controller data path must not contain traversal or symlinks")
        self.data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.data_root.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("Controller data root must be owned and private (0700)")
        self.mind_store = mind_store
        self.sir_profile = sir_profile
        self.knowledge_graph = knowledge_graph
        self.cache_path = self.data_root / "answer_cache.db"
        self.weights_path = self.data_root / "controller_weights.json"
        self.budget_path = self.data_root / "effort_budget.json"
        self.log_path = self.data_root / "controller_log.jsonl"
        self.registry = ModelRegistry(self.data_root)
        with self._locked():
            self._initialize_storage()

    def _initialize_storage(self) -> None:
        for path in (self.cache_path, self.log_path):
            os.close(self._open_private(path, os.O_RDWR | os.O_CREAT))
        if not self.weights_path.exists():
            self._create_json(self.weights_path, DEFAULT_WEIGHTS)
        if not self.budget_path.exists():
            self._create_json(
                self.budget_path,
                {
                    "date": self._now().date().isoformat(),
                    "used": 0,
                    "daily_limit": 10,
                },
            )
        self.weights = self._validated_weights(self._read_json(self.weights_path))
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS answers (
                    id TEXT PRIMARY KEY, question TEXT NOT NULL, normalized TEXT NOT NULL,
                    answer TEXT NOT NULL, effort TEXT NOT NULL, context_key TEXT NOT NULL,
                    belief_key TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS answer_lookup ON answers(context_key, belief_key);
                CREATE TABLE IF NOT EXISTS dialogue (
                    conversation_id TEXT PRIMARY KEY, question TEXT NOT NULL,
                    rounds INTEGER NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS queued (
                    id TEXT PRIMARY KEY, question TEXT NOT NULL, conversation_id TEXT,
                    history TEXT NOT NULL, ready_at TEXT NOT NULL, status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    id TEXT PRIMARY KEY, body TEXT NOT NULL, feedback INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS executions (
                    decision_id TEXT PRIMARY KEY, claimed_at TEXT NOT NULL
                );
            """)

    @contextmanager
    def _locked(self) -> Generator[None]:
        # A separate, stable inode locks atomic JSON replacements across processes.
        fd = self._open_private(self.data_root / "controller.lock", os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @contextmanager
    def _db(self) -> Generator[sqlite3.Connection]:
        os.close(self._open_private(self.cache_path, os.O_RDWR))
        with closing(sqlite3.connect(self.cache_path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    @staticmethod
    def _validated_weights(value: dict[str, Any]) -> dict[str, float]:
        result: dict[str, float] = {}
        for key in DEFAULT_WEIGHTS:
            number = value.get(key)
            if (
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or not math.isfinite(number)
                or not 0.01 <= number <= 2
            ):
                raise ValueError("Invalid controller scoring weights")
            result[key] = float(number)
        total = sum(result.values())
        if abs(total - 1) < 1e-12:
            return result
        return {key: number / total for key, number in result.items()}

    def _write_json(self, path: Path, body: dict[str, Any]) -> None:
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            fd = self._open_private(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(fd, "w") as stream:
                json.dump(body, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    # English sir_profile keys → Chinese equivalents for matching
    # IMPORTANT: longer alternatives must come before shorter ones (regex alternation is greedy-first)
    _ATTR_ZH = {
        "name": "名字|姓名|叫什么名字|叫什么",
        "age": "年龄|岁数|多大了|多大|几岁",
        "phone": "手机号码|手机号|手机|电话号码|电话",
        "account": "账户名|账户|账号",
        "device": "设备|手表|佩戴",
        "personality": "性格|个性|人格",
        "language": "语言|说话方式",
        "father": "父亲|爸爸|爸",
        "mother": "母亲|妈妈|妈",
        "relationship": "称呼|关系|叫我什么",
    }

    @staticmethod
    def _normal(text: str) -> str:
        text = text.casefold()
        for source, target in (
            ("居住地", "住址"),
            ("地址", "住址"),
            ("哪儿", "哪里"),
            ("什么地方", "哪里"),
            ("为什么", "为何"),
            ("如何", "怎么"),
        ):
            text = text.replace(source, target)
        text = re.sub(r"^(?:请问|请|麻烦|能否|可以|帮我|告诉我)+", "", text)
        # Never erase decimal points, arithmetic signs or other meaningful syntax.
        return re.sub(r"\s+", "", text).rstrip("。！？?!；;")

    @classmethod
    def _tokens(cls, text: str) -> set[str]:
        value = cls._normal(text)
        return {value[i : i + 2] for i in range(max(1, len(value) - 1))}

    @classmethod
    def _similarity(cls, left: str, right: str) -> float:
        # Only known concept/polite-form variants may hit. Character overlap is
        # not semantic equivalence: one Chinese entity can change an entire answer.
        if re.findall(r"[A-Za-z_][A-Za-z_0-9]*", left) != re.findall(
            r"[A-Za-z_][A-Za-z_0-9]*", right
        ):
            return 0.0
        return 1.0 if cls._normal(left) == cls._normal(right) else 0.0

    @staticmethod
    def _safety(text: str) -> bool:
        return any(
            word in text for word in ("删除", "覆写", "覆盖", "重置", "清空", "格式化")
        ) or bool(
            re.search(
                r"\b(?:delete|overwrite|reset|truncate|format|drop\s+(?:table|database)|rm)\b",
                text,
                re.I,
            )
        )

    @staticmethod
    def _urgent(text: str) -> bool:
        if re.search(
            r"非紧急|不紧急|不是紧急|无需紧急|并不紧急|not\s+urgent|not\s+an?\s+emergency",
            text,
            re.I,
        ):
            return False
        return bool(
            re.search(r"紧急|急救|胸痛|呼吸困难|火灾|救命|\b(?:urgent|emergency)\b", text, re.I)
        )

    @staticmethod
    def _read_only(text: str) -> bool:
        if ThinkController._safety(text):
            return False
        if re.search(
            r"发送|转账|购买|付款|支付|下单|上传|下载|发布|执行|运行|安装|卸载|"
            r"写入|保存|创建|新建|修改|更新|关闭|开启|启动|停止|移动|复制|预约|取消|"
            r"\b(?:send|transfer|buy|pay|upload|download|publish|execute|run|install|"
            r"save|create|modify|update|start|stop|move|copy|book|cancel)\b",
            text,
            re.I,
        ):
            return False
        return bool(
            re.search(
                r"[?？]|什么|怎么|如何|为何|为什么|分析|解释|计算|比较|推导|证明|"
                r"\b(?:what|why|how|explain|analy[sz]e|compare|calculate|prove)\b",
                text,
                re.I,
            )
        )

    @staticmethod
    def _dynamic(text: str) -> bool:
        return bool(
            re.search(
                r"现在|此刻|当前|今天|明天|昨天|最近|最新|实时|天气|股价|"
                r"\b(?:now|today|latest|current|weather)\b",
                text,
                re.I,
            )
        )

    def score_difficulty(self, question: str) -> dict[str, Any]:
        """Bounded pure-local five-feature score; no I/O, embeddings or model calls."""
        started = time.perf_counter()
        text = question[:16000]
        inference = len(
            re.findall(
                r"分析|推理|推导|证明|解释|机制|因果|为何|为什么|设计|容错|权衡|"
                r"\b(?:analy[sz]e|reason|prove|infer|design|explain|causal|mechanism)\b",
                text,
                re.I,
            )
        )
        steps = len(
            re.findall(
                r"首先|然后|接着|最后|并且|以及|并|再|同时|步骤|方案|\b(?:then|and|steps|plan|first|finally)\b",
                text,
                re.I,
            )
        )
        density = len(set(re.findall(r"[\u4e00-\u9fff]|[a-z]+|\d+", text.casefold())))
        features = {
            "length": min(1.0, len(text) / 400),
            "density": min(1.0, density / 80),
            "emotion": min(
                1.0,
                len(
                    re.findall(
                        r"害怕|焦虑|难过|愤怒|痛苦|救命|[!！]|\b(?:afraid|angry|anxious|panic)\b",
                        text,
                        re.I,
                    )
                )
                / 2,
            ),
            "inference": min(1.0, inference / 3),
            "multistep": min(1.0, steps / 2),
        }
        score = sum(features[key] * self.weights[key] for key in features)
        effort = "high" if score >= 0.55 else "medium" if score >= 0.22 else "low"
        return {
            "features": features,
            "score": score,
            "effort": effort,
            "duration_ms": (time.perf_counter() - started) * 1000,
        }

    def _beliefs(self) -> tuple[list[dict[str, Any]], str]:
        beliefs: list[dict[str, Any]] = []
        key = "empty"
        incomplete = False
        if self.mind_store is not None:
            # Use the store's connection contract, but bound results. Do not read
            # the unbounded beliefs()/entries() history on every question.
            with self.mind_store.connection() as db:
                rows = db.execute(
                    "SELECT b.* FROM belief_versions b WHERE NOT EXISTS ("
                    "SELECT 1 FROM belief_versions newer WHERE newer.belief_id=b.belief_id "
                    "AND newer.seq>b.seq) ORDER BY b.seq DESC LIMIT 513"
                ).fetchall()
                latest = db.execute("SELECT COALESCE(MAX(seq),0) FROM belief_versions").fetchone()[
                    0
                ]
            beliefs = [dict(row) for row in rows[:512]]
            key = str(latest)
            incomplete = len(rows) > 512
        if self.sir_profile is not None:
            facts = self.sir_profile.get_high_confidence(threshold=0.8)
            for fact in facts:
                beliefs.append(
                    {
                        "belief_id": "sir_profile:" + fact["id"],
                        "subject": fact.get("category") or "Sir",
                        "attribute": fact["key"],
                        "value": fact["value"],
                        "confidence": fact["confidence"],
                        "source": "sir_profile",
                        "original_source": fact["source"],
                        "updated_at": fact["updated_at"],
                    }
                )
            # Source timestamps, never wall-clock time or file atime: unchanged
            # reads must keep cache hits. Hash the snapshot too so removals and
            # updates below the newest timestamp cannot retain stale answers.
            updated_at = max((fact["updated_at"] for fact in facts), default="empty")
            snapshot = json.dumps(
                sorted(facts, key=lambda fact: fact["id"]),
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
            key += (
                ":sir_profile:" + updated_at + ":" + hashlib.sha256(snapshot.encode()).hexdigest()
            )
        # Keep this marker last: routing and cache admission use endswith().
        return beliefs, key + (":incomplete" if incomplete else "")

    @classmethod
    def _facet_match(cls, question: str, belief: dict[str, Any]) -> bool:
        q = cls._normal(question)
        subject = cls._normal(str(belief["subject"]))
        attribute = cls._normal(str(belief["attribute"]))
        owner = belief.get("source") == "sir_profile" or cls._subject_key(subject) == "owner"
        relevant = attribute in q or (("住" in attribute or "地址" in attribute) and "住在" in q)
        return bool(
            attribute
            and relevant
            and (subject in q or (owner and any(x in q for x in ("我", "本人", "sir", "我的"))))
        )

    @staticmethod
    def _subject_key(subject: str) -> str:
        subject = subject.casefold()
        return "owner" if subject in {"sir", "user", "owner", "用户", "我", "您", "你"} else subject

    @classmethod
    def _direct_fact_query(cls, question: str, belief: dict[str, Any]) -> bool:
        q = cls._normal(question)
        raw_attr = str(belief["attribute"])
        attribute = cls._normal(raw_attr)
        subject = re.escape(cls._normal(str(belief["subject"])))

        # Build attribute pattern: raw key + any Chinese synonyms registered in _ATTR_ZH
        zh_synonyms = cls._ATTR_ZH.get(raw_attr.lower(), "")
        attr_pattern = re.escape(attribute)
        if zh_synonyms:
            attr_pattern = "(?:" + attr_pattern + "|" + zh_synonyms + ")"

        if belief.get("source") == "sir_profile":
            subject = r"(?:我|sir|用户|" + subject + ")"
        elif cls._subject_key(str(belief["subject"])) == "owner":
            subject = r"(?:我|sir|用户)"

        # Standard "X的Y是什么" pattern
        standard = bool(
            re.fullmatch(
                subject + r"的?" + attr_pattern + r"(?:是什么|是哪里|在哪里|是多少|多少|是啥)?", q
            )
        )
        if standard:
            return True

        # Natural patterns: only match if a synonym specific to THIS attribute appears
        if belief.get("source") == "sir_profile" and zh_synonyms:
            if re.search(zh_synonyms, q):
                return True

        return False

    def _conflict(self, question: str, beliefs: list[dict[str, Any]]) -> str | None:
        matched = [b for b in beliefs if self._facet_match(question, b) and b["confidence"] > 0.5]
        groups: dict[tuple[str, str], set[str]] = {}
        for b in matched:
            owner = (
                b.get("source") == "sir_profile" or self._subject_key(str(b["subject"])) == "owner"
            )
            facet = (
                "owner" if owner else self._subject_key(str(b["subject"])),
                self._normal(str(b["attribute"])),
            )
            groups.setdefault(facet, set()).add(b["value"])
            value = str(b["value"])
            if re.search(r"(?:不是|不在|并非|不住在|不住|非)\s*" + re.escape(value), question):
                return f"premise_negates_belief:{b['belief_id']}:{value}"
            subject = re.escape(str(b["subject"]))
            if owner:
                subject = r"(?:我|Sir|sir|用户|" + subject + ")"
            attribute = re.escape(str(b["attribute"]))
            pattern = subject + r"(?:的)?(?:" + attribute + r")(?:是|为|在|=|：|:)\s*(.+)"
            patterns = [pattern]
            if "住" in str(b["attribute"]) or "地址" in str(b["attribute"]):
                patterns.append(subject + r"(?:现在)?住在\s*(.+)")
            for clause in re.split(r"[，。！？?;；,]", question):
                if re.search(
                    r"如果|假设|是否|是不是|吗|什么|哪里|多少|\b(?:if|suppose|whether|what|where)\b",
                    clause,
                    re.I,
                ):
                    continue
                for p in patterns:
                    assertion = re.search(p, clause)
                    if assertion and self._normal(assertion[1]) != self._normal(value):
                        return f"premise_differs_from_belief:{b['belief_id']}:{value}"
        for (subject, attribute), values in groups.items():
            if len(values) > 1:
                return f"inconsistent_beliefs:{subject}:{attribute}:{'|'.join(sorted(values))}"
        return None

    def _memory_answer(self, question: str, beliefs: list[dict[str, Any]]) -> str | None:
        if self._dynamic(question):
            return None
        matches = [
            b for b in beliefs if b["confidence"] > 0.95 and self._direct_fact_query(question, b)
        ]
        if len(matches) == 1:
            return str(matches[0]["value"])
        # A bare exact key is a read too; adding punctuation here only admits
        # retrieval, while whole-question validation below still governs answers.
        if self.knowledge_graph is not None and self._read_only(question + "?"):
            try:
                candidates: list[dict[str, Any]] = []
                for fact in self.knowledge_graph.query(question):
                    if fact.get("node_type") != "fact" or not 0.85 < fact.get("confidence", 0) <= 1:
                        continue
                    candidate = {
                        "belief_id": "knowledge_graph:" + str(fact.get("id", "")),
                        "subject": fact.get("domain") or "Sir",
                        "attribute": fact["key"],
                        "value": fact["value"],
                        "confidence": fact["confidence"],
                        "source": "sir_profile"
                        if fact.get("origin_table") == "sir_profile"
                        else "knowledge_graph",
                    }
                    if not isinstance(candidate["attribute"], str) or not candidate["attribute"]:
                        continue
                    if not isinstance(candidate["value"], str) or not candidate["value"]:
                        continue
                    # query() is lexical OR retrieval, not a QA oracle. Require
                    # the same whole-question match as beliefs, or an exact key
                    # lookup; related facets and action requests must fall through.
                    if self._direct_fact_query(question, candidate) or self._normal(
                        question
                    ) == self._normal(candidate["attribute"]):
                        candidates.append(candidate)
                if self._conflict(question, beliefs + candidates):
                    return None
                values = {candidate["value"] for candidate in candidates}
                if len(values) == 1:
                    return values.pop()
            except Exception:
                # Optional graph retrieval must never prevent normal routing.
                return None
        return None

    @staticmethod
    def _context_key(history: list[dict[str, Any]] | None) -> str:
        # Context-sensitive answers are never shared between different histories.
        raw = json.dumps(history or [], ensure_ascii=False, sort_keys=True, allow_nan=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def _cache_answer(
        self, question: str, history: list[dict[str, Any]] | None, belief_key: str
    ) -> str | None:
        if self._dynamic(question) or not self._read_only(question):
            return None
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM answers WHERE context_key=? AND belief_key=? AND expires_at>? "
                "ORDER BY created_at DESC LIMIT 256",
                (self._context_key(history), belief_key, self._now().isoformat()),
            ).fetchall()
        for row in rows:
            if self._similarity(question, row["question"]) >= 0.93:
                return str(row["answer"])
        return None

    def cache_answer(
        self,
        question: str,
        answer: str,
        effort: str,
        *,
        history: list[dict[str, Any]] | None = None,
        ttl_seconds: int = 86400,
    ) -> str:
        """Cache a caller-verified successful extreme answer, never an operation.

        Similarity uses conservative local concept normalization, not embeddings.
        Arbitrary paraphrases deliberately miss rather than guess.
        """
        if effort not in EXTREME or not question.strip() or not answer.strip() or ttl_seconds <= 0:
            raise ValueError(
                "Only nonempty successful extreme answers with positive TTL may be cached"
            )
        if not self._read_only(question) or self._dynamic(question):
            raise ValueError("Only read-only, time-independent questions may be cached")
        with self._locked():
            beliefs, key = self._beliefs()
            if key.endswith(":incomplete") or self._conflict(question, beliefs):
                raise ValueError("Conflicting or incompletely checked premises cannot be cached")
            identity = uuid.uuid4().hex
            now = self._now()
            with self._db() as db:
                db.execute(
                    "INSERT INTO answers VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        identity,
                        question,
                        self._normal(question),
                        answer,
                        effort,
                        self._context_key(history),
                        key,
                        now.isoformat(),
                        (now + timedelta(seconds=ttl_seconds)).isoformat(),
                    ),
                )
            return identity

    def _depth(
        self,
        question: str,
        conversation_id: str | None,
        history: list[dict[str, Any]] | None,
        *,
        record: bool,
    ) -> int:
        users = [
            str(h.get("content", h.get("text", "")))
            for h in (history or [])
            if h.get("role") == "user"
        ]
        rounds = 1
        anchor = question
        followup = bool(
            re.fullmatch(
                r"(?:那|那么)?(?:继续|再讲讲|进一步说说|还有呢|为什么|为何|怎么做|如何做|why|continue)[？?！!。\s]*",
                question,
                re.I,
            )
        )
        for previous in reversed(users[-64:]):
            if self._same_topic(anchor, previous) or (followup and rounds == 1):
                rounds += 1
                anchor = previous
            else:
                break
        if conversation_id and record:
            with self._db() as db:
                row = db.execute(
                    "SELECT * FROM dialogue WHERE conversation_id=?", (conversation_id,)
                ).fetchone()
                if (
                    row
                    and (self._now() - datetime.fromisoformat(row["updated_at"])).total_seconds()
                    < 86400
                ):
                    if self._same_topic(question, row["question"]) or followup:
                        rounds = max(rounds, row["rounds"] + 1)
                db.execute(
                    "INSERT OR REPLACE INTO dialogue VALUES (?,?,?,?)",
                    (conversation_id, question, rounds, self._now().isoformat()),
                )
        return rounds

    @classmethod
    def _same_topic(cls, a: str, b: str) -> bool:
        x, y = cls._tokens(a), cls._tokens(b)
        return len(x & y) / max(1, min(len(x), len(y))) >= 0.35

    def _budget(self, consume: bool) -> dict[str, Any]:
        body = self._read_json(self.budget_path)
        limit, used = body.get("daily_limit", 10), body.get("used", 0)
        if type(limit) is not int or type(used) is not int or limit < 0 or used < 0:
            raise ValueError("Invalid extreme budget")
        day = self._now().date().isoformat()
        if body.get("date") != day:
            used = 0
        allowed = used < limit
        if consume and allowed:
            used += 1
        self._write_json(self.budget_path, {"date": day, "used": used, "daily_limit": limit})
        return {"allowed": allowed, "used": used, "daily_limit": limit}

    def set_daily_limit(self, daily_limit: int) -> None:
        if type(daily_limit) is not int or daily_limit < 0:
            raise ValueError("daily_limit must be a nonnegative integer")
        with self._locked():
            body = self._read_json(self.budget_path)
            body["daily_limit"] = daily_limit
            self._write_json(self.budget_path, body)

    @staticmethod
    def _extreme(question: str, inconsistent: bool, scored: dict[str, Any]) -> str | None:
        domains = (
            r"量子|物理|纠缠|quantum|physics",
            r"数学|定理|拓扑|数学证明|mathemat|theorem",
            r"计算机|算法|软件|数据库|计算复杂|algorithm|software|database",
            r"生物|基因|神经|biology|gene|neuro",
            r"经济|金融|金融学|econom|financ",
            r"心理|认知|心智|psycholog|cognit",
            r"伦理|哲学|道德|ethic|philosoph",
        )
        crossed = sum(bool(re.search(p, question, re.I)) for p in domains) >= 2
        original = bool(
            re.search(
                r"原创|创新|新理论|提出|设计|发明|original|novel|invent|design", question, re.I
            )
        )
        if not (crossed and original and inconsistent and scored["features"]["inference"] > 0):
            return None
        if re.search(r"严格证明|形式化证明|形式验证|极限|ultra|formal proof", question, re.I):
            return "ultra"
        return "max" if scored["effort"] == "high" else "xhigh"

    def _queue(
        self, question: str, conversation_id: str | None, history: list[dict[str, Any]] | None
    ) -> str:
        ready = self._now().replace(hour=7, minute=30, second=0, microsecond=0)
        key = hashlib.sha256(
            json.dumps(
                [question, conversation_id, history or []], ensure_ascii=False, sort_keys=True
            ).encode()
        ).hexdigest()
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO queued VALUES (?,?,?,?,?,?)",
                (
                    key,
                    question,
                    conversation_id,
                    json.dumps(history or [], ensure_ascii=False),
                    ready.isoformat(),
                    "pending",
                ),
            )
        return key

    def release_queued(self) -> list[ThinkDecision]:
        """Re-evaluate ready work in active hours; queued work reserves no budget."""
        with self._locked():
            with self._db() as db:
                rows = db.execute(
                    "SELECT * FROM queued WHERE status='pending' AND ready_at<=?",
                    (self._now().isoformat(),),
                ).fetchall()
            results: list[ThinkDecision] = []
            for row in rows:
                result = self._route(
                    row["question"],
                    row["conversation_id"],
                    json.loads(row["history"]),
                    record_depth=False,
                )
                if not result.queued:
                    with self._db() as db:
                        db.execute("UPDATE queued SET status='released' WHERE id=?", (row["id"],))
                results.append(result)
            return results

    @staticmethod
    def _now() -> datetime:
        return datetime.now(CST)

    @staticmethod
    def _open_private(path: Path, flags: int) -> int:
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            os.close(fd)
            raise ValueError("Controller storage must be owned regular files without links")
        os.fchmod(fd, 0o600)
        return fd

    def _create_json(self, path: Path, body: dict[str, Any]) -> None:
        try:
            fd = self._open_private(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            return
        with os.fdopen(fd, "w") as stream:
            json.dump(body, stream, ensure_ascii=False, allow_nan=False)

    def _read_json(self, path: Path) -> dict[str, Any]:
        with os.fdopen(self._open_private(path, os.O_RDONLY), "r") as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError("Controller state must be a JSON object")
        return cast(dict[str, Any], value)

    def _finish(
        self,
        decision: ThinkDecision,
        question: str,
        started: float,
        trace: list[dict[str, Any]],
        conversation_id: str | None,
    ) -> ThinkDecision:
        if not decision.bypass_model and not decision.queued:
            model = select_model(classify_task(question), self.registry)
            model = model or select_model("general", self.registry)
            decision.model_id = model["id"] if model else None
        decision.log_entry = {
            "id": uuid.uuid4().hex,
            "timestamp": self._now().isoformat(),
            "conversation_id": conversation_id,
            "question": question,
            **{k: v for k, v in asdict(decision).items() if k != "log_entry"},
            "trace": trace,
            "duration_ms": (time.perf_counter() - started) * 1000,
        }
        with self._db() as db:
            db.execute(
                "INSERT INTO decisions(id,body) VALUES (?,?)",
                (
                    decision.log_entry["id"],
                    json.dumps(decision.log_entry, ensure_ascii=False, allow_nan=False),
                ),
            )
        self._append_log(decision.log_entry)
        return decision

    def _append_log(self, entry: dict[str, Any]) -> None:
        with os.fdopen(self._open_private(self.log_path, os.O_WRONLY | os.O_APPEND), "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n")

    def route(
        self,
        question: str,
        conversation_id: str | None = None,
        history: list[dict[str, Any]] | None = None,
    ) -> ThinkDecision:
        """Apply all ten layers locally. The lock makes budget/depth updates atomic."""
        if not question.strip():
            raise ValueError("question must be nonempty text")
        if len(question) > 16000:
            raise ValueError("question exceeds the 16000-character local-policy bound")
        with self._locked():
            return self._route(question, conversation_id, history, record_depth=True)

    def _route(
        self,
        question: str,
        conversation_id: str | None,
        history: list[dict[str, Any]] | None,
        *,
        record_depth: bool,
    ) -> ThinkDecision:
        started = time.perf_counter()
        self.weights = self._validated_weights(self._read_json(self.weights_path))
        decision = ThinkDecision(layer="difficulty")
        trace: list[dict[str, Any]] = []
        safety = self._safety(question)
        decision.needs_critic = safety
        trace.append(
            {"layer": "safety_gate", "hit": safety, "minimum": "medium" if safety else "low"}
        )
        beliefs, belief_key = self._beliefs()
        # A candidate shortcut is provisional until consistency has vetoed it.
        complete = not belief_key.endswith(":incomplete")
        answer = None if safety or not complete else self._memory_answer(question, beliefs)
        trace.append(
            {
                "layer": "memory_shortcut",
                "candidate": answer is not None,
                "belief_scan_complete": complete,
            }
        )
        conflict = self._conflict(question, beliefs)
        if not complete:
            conflict = conflict or "belief_scan_incomplete:require_model_retrieval"
        decision.conflict = conflict
        trace.append({"layer": "consistency", "hit": conflict is not None, "conflict": conflict})
        if conflict:
            answer = None
            decision.layer = "consistency"
        if answer is not None:
            decision.layer = "memory_shortcut"
            decision.bypass_model, decision.answer = True, answer
        cached = None
        if not (safety or conflict or decision.bypass_model):
            cached = self._cache_answer(question, history, belief_key)
        trace.append(
            {"layer": "cache", "hit": cached is not None, "matcher": "local_concept_equivalence"}
        )
        if cached is not None:
            decision.layer = "cache"
            decision.bypass_model, decision.answer = True, cached
        bypass = decision.bypass_model
        scored = self.score_difficulty(question)
        trace.append({"layer": "difficulty", "skipped": bypass, **scored})
        if not bypass:
            decision.effort = scored["effort"]
        rounds = self._depth(question, conversation_id, history, record=record_depth)
        deeper = rounds > 3 and not bypass
        # Depth can raise to high only; extreme use MUST pass its own trigger and budget.
        if deeper and decision.effort in ("low", "medium"):
            decision.effort = EFFORTS[EFFORTS.index(decision.effort) + 1]
            decision.layer = "depth"
        trace.append(
            {
                "layer": "depth",
                "rounds": rounds,
                "hit": deeper,
                "extreme_requires_separate_gate": True,
            }
        )
        historical = bool(conflict) or bool(
            re.search(
                r"前后不一|历史不一致|自相矛盾|之前.*(?:错|矛盾)|与.*(?:冲突|矛盾)|"
                r"\b(?:inconsistent|contradiction|contradictory)\b",
                question,
                re.I,
            )
        )
        # Structured historical conflicts are evidence, not instructions.
        historical = historical or any(
            bool(h.get("conflict")) or bool(h.get("log_entry", {}).get("conflict"))
            for h in (history or [])[-32:]
            if isinstance(h.get("log_entry", {}), dict)
        )
        extreme = None if bypass else self._extreme(question, historical, scored)
        budget: dict[str, Any] | None = self._budget(False) if extreme else None
        if extreme and budget and budget["allowed"]:
            decision.effort, decision.layer = extreme, "extreme"
        elif extreme:
            decision.effort, decision.layer = "high", "fallback"
        trace.append(
            {
                "layer": "extreme",
                "candidate": extreme,
                "budget": budget,
                "hit": bool(extreme and budget and budget["allowed"]),
            }
        )
        now = self._now()
        quiet = now.hour * 60 + now.minute < 450
        if decision.effort in EXTREME and quiet and not self._urgent(question):
            decision.queued = True
            decision.layer = "quiet"
            queue_id = self._queue(question, conversation_id, history)
        else:
            queue_id = None
            if decision.effort in EXTREME:
                budget = self._budget(True)
        trace.append(
            {
                "layer": "quiet",
                "quiet_hours": quiet,
                "hit": decision.queued,
                "queue_id": queue_id,
                "budget": budget,
                "budget_reserved": not decision.queued and decision.effort in EXTREME,
            }
        )
        if safety:
            if EFFORTS.index(decision.effort) < EFFORTS.index("medium"):
                decision.effort = "medium"
            if decision.layer not in ("quiet", "extreme", "fallback"):
                decision.layer = "safety_gate"
        trace.append(
            {
                "layer": "fallback",
                "chain": self.fallback_chain(decision.effort),
                "retry_only_explicitly_safe_failures": True,
            }
        )
        trace.append(
            {
                "layer": "observability",
                "hit": True,
                "model_tokens": 0,
                "feedback_hook": "record_feedback",
                "belief_key": belief_key,
            }
        )
        return self._finish(decision, question, started, trace, conversation_id)

    @staticmethod
    def fallback_chain(effort: str) -> list[str]:
        if effort not in EFFORTS:
            raise ValueError("Unknown reasoning effort")
        if effort in ("low", "medium"):
            return [effort]
        return list(reversed(EFFORTS[1 : EFFORTS.index(effort) + 1]))

    def _claim_execution(self, decision: ThinkDecision) -> None:
        """Locked, one-shot admission. Unknown outcomes remain claimed forever."""
        identity = decision.log_entry.get("id")
        with self._db() as db:
            row = db.execute("SELECT body FROM decisions WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise ValueError("Execution requires a persisted controller decision")
            stored = json.loads(row["body"])
            for key in ("effort", "model_id", "needs_critic", "bypass_model", "queued", "conflict"):
                if getattr(decision, key) != stored[key]:
                    raise ValueError("Decision was modified after routing")
            if datetime.fromisoformat(stored["timestamp"]).date() != self._now().date():
                raise ValueError("Decision expired; route again for current-day admission")
            quiet = self._now().hour * 60 + self._now().minute < 450
            if decision.effort in EXTREME and quiet and not self._urgent(stored["question"]):
                raise ValueError("Extreme execution is blocked during quiet hours; route again")
            try:
                db.execute(
                    "INSERT INTO executions VALUES (?,?)", (identity, self._now().isoformat())
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Decision was already executed or has an unknown outcome") from exc

    def execute_with_fallback(
        self, decision: ThinkDecision, execute: Callable[[ThinkDecision], Any]
    ) -> Any:
        """Execute caller callback; retry ONLY explicitly safe RetryableThinkError.

        Generic exceptions, timeouts and unknown outcomes propagate without retry.
        The adapter must raise RetryableThinkError only for a proven rejected call,
        never after a possibly executed destructive action or unknown billing.
        """
        if decision.queued:
            raise ValueError("Queued work must first be released in active hours")
        if decision.bypass_model:
            return decision.answer
        current = decision
        chain = self.fallback_chain(decision.effort)
        for index, effort in enumerate(chain):
            if index:
                with self._locked():
                    if effort in EXTREME and not self._budget(True)["allowed"]:
                        continue
                    current = ThinkDecision(
                        layer="fallback",
                        effort=effort,
                        needs_critic=decision.needs_critic,
                        conflict=decision.conflict,
                    )
                    self._finish(
                        current,
                        str(decision.log_entry.get("question", "")),
                        time.perf_counter(),
                        [
                            {
                                "layer": "fallback",
                                "previous": chain[index - 1],
                                "parent_id": decision.log_entry.get("id"),
                            }
                        ],
                        decision.log_entry.get("conversation_id"),
                    )
            with self._locked():
                self._claim_execution(current)
            try:
                return execute(current)
            except RetryableThinkError:
                if index == len(chain) - 1:
                    raise
        raise RuntimeError("No eligible fallback route")

    def record_feedback(self, decision_id: str, behavior: str) -> dict[str, float]:
        """Learn from explicit caller-observed behavior; one feedback per decision.

        accepted/abandoned are recorded but not assumed to imply difficulty.
        correction/followup/too_shallow increase active-feature weights;
        too_verbose reduces them. No safety floor, budget or quiet rule is learned.
        """
        if behavior not in {
            "accepted",
            "abandoned",
            "correction",
            "followup",
            "too_shallow",
            "too_verbose",
        }:
            raise ValueError("Unknown feedback behavior")
        with self._locked():
            with self._db() as db:
                row = db.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
                if row is None:
                    raise ValueError("Unknown decision id")
                if row["feedback"]:
                    return self._validated_weights(self._read_json(self.weights_path))
                entry = json.loads(row["body"])
                features: dict[str, float] = next(
                    (s["features"] for s in entry["trace"] if "features" in s), dict[str, float]()
                )
                weights = self._validated_weights(self._read_json(self.weights_path))
                delta = (
                    0.08
                    if behavior in {"correction", "followup", "too_shallow"}
                    else -0.08
                    if behavior == "too_verbose"
                    else 0
                )
                for key in weights:
                    weights[key] = max(
                        0.02, min(0.8, weights[key] * (1 + delta * features.get(key, 0)))
                    )
                weights = self._validated_weights(weights)
                self._write_json(self.weights_path, weights)
                db.execute("UPDATE decisions SET feedback=1 WHERE id=?", (decision_id,))
            self.weights = weights
            self._append_log(
                {
                    "kind": "feedback",
                    "decision_id": decision_id,
                    "behavior": behavior,
                    "weights": weights,
                    "timestamp": self._now().isoformat(),
                }
            )
            return dict(weights)


class RetryableThinkError(RuntimeError):
    """Caller-certified rejected request with no execution and no unknown outcome."""
