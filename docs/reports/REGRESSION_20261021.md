# 全量回归记录 2026-10-21

- 代码版本：HEAD `e17d46e7`（打标签 `review-20261021`）
- 命令：`TMPDIR=$HOME/.tmp-pytest PYTHONPATH=packaging .venv/bin/python3 -m pytest packaging/ -q --ignore=packaging/soak`
- 结果：**1578 passed, 1091 subtests passed, 0 failed**，用时 11 分 09 秒
- 未纳入：`packaging/soak/`（长跑脚本，不是单元测试，单独跑会挂在收集阶段）
- 说明：本次回归前修复了 24 条历史失败用例，其中 4 条暴露了真实产品缺陷（见提交 `e17d46e7`）
