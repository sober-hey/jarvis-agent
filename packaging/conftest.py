"""pytest 引导：让 packaging/ 的测试能找到隔离出去的语音子系统。

语音模块已从 packaging/ 迁到仓库根 voice/（见 voice/README.md）。
这里把 voice/ 追加到 sys.path，保持既有 `import voice`、`from voice_x import ...`
写法不变；追加而不是前插，避免 voice/ 下的同名文件盖住 packaging/ 的模块。
"""

from __future__ import annotations

import sys
from pathlib import Path

VOICE_DIR = Path(__file__).resolve().parent.parent / "voice"
if VOICE_DIR.is_dir() and str(VOICE_DIR) not in sys.path:
    sys.path.append(str(VOICE_DIR))
