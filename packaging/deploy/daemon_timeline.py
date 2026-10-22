"""Publish an existing local outreach delivery into the one public timeline."""

from __future__ import annotations

import argparse
import importlib
import json
import sqlite3
import sys
import sysconfig
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine_entry as engine  # noqa: E402
import product_home  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--delivery-id", required=True, type=int)
    args = parser.parse_args()
    root = product_home.validate_root(args.data_root)
    timeline: Any = json.loads((root / "timeline.json").read_text())["session_id"]
    metadata: dict[str, Any] = {}
    with sqlite3.connect((root / "outreach.db").as_uri() + "?mode=ro", uri=True) as db:
        delivery = db.execute(
            "SELECT key,text,channel FROM deliveries WHERE id=?", (args.delivery_id,)
        ).fetchone()
        if delivery is not None:
            row = db.execute(
                "SELECT payload FROM delivery_metadata WHERE key=?", (delivery[0],)
            ).fetchone()
            if row:
                metadata = json.loads(row[0])
    if delivery is None:
        raise ValueError("delivery not found")
    engine.interpreter_check()
    engine.verify_vendor()
    sys.path.append(str(Path(sysconfig.get_path("purelib"))))
    sys.path.insert(0, str(engine.VENDOR))
    with engine.ProductRuntimeAdapter():
        factory = importlib.import_module("hermes_state").SessionDB
    store = factory(db_path=root / "engine-home/state.db")
    try:
        store.create_session(timeline, "jarvis-text-chat")
        with sqlite3.connect((root / "engine-home/state.db").as_uri() + "?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT display_metadata FROM messages WHERE session_id=?", (timeline,)
            ).fetchall()
        if any(
            json.loads(row[0] or "{}").get("outreach_delivery_key") == delivery[0] for row in rows
        ):
            return 0
        channel = delivery[2] if delivery[2] in {"email", "voice", "imessage"} else "web"
        store.append_messages_batch(
            timeline,
            [
                {
                    "role": "assistant",
                    "content": delivery[1],
                    "display_metadata": {
                        "source_channel": "jarvis_proactive",
                        "delivery_channel": channel,
                        **metadata,
                        "outreach_delivery_key": delivery[0],
                    },
                }
            ],
        )
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
