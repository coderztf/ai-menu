# 004 Python 工程目录重构

将运行代码组织到 src/ai_menu 包，H5 静态资源作为包内资源，tests 保持独立。通过 pyproject.toml 声明依赖和包资源，支持可编辑安装。AI YAML 与数据库继续存放项目根目录，保留现有数据和配置值。

验收要求：安装后可导入 ai_menu.api；VS Code 使用 ai_menu.api:app 启动；从其他工作目录导入时仍定位项目根目录的配置和数据库；静态首页与原有自动化测试通过。样本导入改用 python -m ai_menu.seed_recipes，初始化 SQL 保持原位置。现有 API、库存、菜单、图片和定时功能保持兼容。
