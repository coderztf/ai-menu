# 实施计划

实现事务迁移执行器和带校验和的迁移记录，纳入现有建表 SQL 为基线。实现自然日日志处理器和 Python 后台入口。启动脚本顺序执行拉取 main、依赖安装、旧进程停止、迁移备份与执行、后台启动及健康检查。用临时数据库验证首次执行、重复执行、失败回滚；静态检查 shell。远程服务器实机部署另行验收。

本地验证通过：shell 语法、新数据库迁移、重复迁移、旧数据库接管、失败迁移回滚、已执行脚本校验和拒绝、自然日日志文件输出。远程 Linux 上的 Git 更新、进程重启及真实跨午夜日志仍待服务器验收。

运行方式为在项目根目录执行 bash scripts/start.sh。首次需先将仓库克隆到 /opt/ai-menu，准备 ai-config.yaml 并确保运行账号有项目及日志目录写权限。服务器需安装 util-linux 的 flock。脚本启动后即退出，服务在后台运行。可通过 MENU_HOST、MENU_PORT、MENU_DATABASE 和 MENU_LOG_DIR 环境变量覆盖默认值。

重复启动增强：将进程停止逻辑提取为 scripts/process.sh，加入项目目录核对、TERM 超时后 KILL、失效 PID 清理和明确进程输出。启动脚本在迁移前调用。验证 shell 语法和模拟正常退出、强制退出、失效 PID 与其他进程等分支。

本次验收通过：start.sh 和 process.sh 的 bash 语法检查；模拟验证失效 PID 清理、其他进程拒绝停止、TERM 正常退出、TERM 超时后 KILL 以及 PID 文件清理。真实 Linux 进程与 /proc 项目目录核对待服务器执行验证。
