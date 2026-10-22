"""让 packaging/soak 下的测试能直接 import run_soak。

这些用例是按"在 soak 目录里跑"写的（`import run_soak as runner`），
从仓库根收集时该目录不在 sys.path 上，会 collection error。
在这里显式加路径，比要求调用方拼 PYTHONPATH 更不容易踩坑。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
