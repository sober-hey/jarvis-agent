"""Owned, offline evaluation surface for the logical iMessage ingress resource.

Actual mailbox polling remains in daemon_wiring.Wiring._poll_imessage. This
module deliberately does NOT import that daemon, open Messages, send receipts,
or pretend to replace its ingestion path. Registry ownership/runtime wiring
must be migrated explicitly before this hook is advertised by the resident.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def self_evaluate() -> dict[str, Any]:
    """Audit ingress boundaries without inspecting personal mailbox contents."""
    return {
        "module": "imessage_input",
        "domain": "direct iMessage inbox ingress resource and provenance boundaries",
        "current_version": "逻辑消息入口的只读自评接口；现有按账号/发件人筛选与 ROWID 游标入 inbox 实现在 daemon_wiring",
        "known_limitations": [
            "实际轮询仍由 daemon_wiring._poll_imessage 实现；本模块 hook 未接入 daemon/注册表，不声称新接线已生效",
            "现有入口只读非空 text 的入站 iMessage；不解码 attributedBody、附件或群聊语义，首次只取最近一条以免重放旧指令",
            "实际读取依赖 macOS Messages 数据库权限及 schema；本 hook 不探测真实消息，不证明权限或收发可用",
            "本模块不写 mind 或 KG、不发送消息；现有 daemon 的 inbox 消费与通知回执属于其他模块，入队不等于送达",
        ],
        "improvement_directions": [
            "先以合成 SQLite 邮箱夹具固化账号/发件人过滤、首次单条、断点续读与入队原子性，再显式迁移入口归属",
            "为 attributedBody/附件增加有界解析及来源标记，不把历史消息自动升级为当前命令",
            "在注册表与 daemon 经独立变更连接 hook 后另验权限和回执，不将本次离线自评当真实收发验收",
        ],
        "last_evaluated_at": datetime.now(UTC).isoformat(),
    }
