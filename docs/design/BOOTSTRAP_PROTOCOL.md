# Bootstrap Protocol

K0-001 只建立工程工具、环境检测和真实测试，不实现执行协议。保留已有文档及契约草案，创建哈希存档后更新管理状态。Git 初始化只允许在不存在原仓库时执行。

所有依赖锁定于 uv.lock，使用 uv sync --frozen。质量门禁包括 Ruff 检查与格式验证、Pyright strict、pytest 和环境测量。测试只使用临时目录和临时数据库。

初始 Git 作者标识为 JARVIS Development Agent（自动开发代理），不冒充项目负责人。Git 提交只记录开发交付，不表示契约或正式发布已获人工批准。

回滚优先使用 Git revert 撤销具体开发提交。初始化前文档保存在 docs/archive/ 的 bootstrap 存档；比对哈希后恢复到独立目录，避免覆盖后续用户修改。运行数据不在仓库内。
