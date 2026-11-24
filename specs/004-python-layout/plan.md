# 实施计划

1. 建立 src/ai_menu 包，迁移现有 Python 模块及 H5，更新包内导入。
2. 集中定义项目路径，更新默认 YAML、数据库和静态资源定位。
3. 增加 pyproject.toml，更新启动配置、依赖入口和测试导入。
4. 可编辑安装并执行原有测试，从项目外检查导入路径。记录验证结果。

 
## 验收结果

- 项目 .venv 中完成可编辑安装，25 项现有自动化测试全部通过。
- 从 /private/tmp 导入应用并验证根目录配置与数据库定位，H5 首页和脚本返回 200。
- VS Code 配置已使用 ai_menu.api:app，实际 IDE F5 点按启动尚未验证。
- 保留根目录的配置、数据库及其图片目录。运行测试仍有既有的 SQLite 连接资源警告及测试客户端弃用警告。
- 安装后命令为 .venv/bin/python -m uvicorn ai_menu.api:app --host 127.0.0.1 --port 8000 --reload。
- 样本导入命令为 .venv/bin/python -m ai_menu.seed_recipes。新环境安装使用 .venv/bin/python -m pip install -e '.[test]'。
