# scripts

Windows 下的参考启动与守护脚本。**所有路径按 `D:\deepsleep` 布局写死**，复用时请改成你自己的路径。

| 文件 | 作用 |
|---|---|
| `start_maibot_silent.bat` | 隐藏窗口启动 MaiBot（经 `hidden_run.vbs`；Windows 下唯一稳定的无窗口方案） |
| `start_maibot.bat` | 直接前台启动（调试用，会弹窗） |
| `start_maibot_wrapper.bat` | 前台启动 + 日志 trace；启动前检查 `:8100`，没起就自动拉起 embedding |
| `stop_all.bat` | 按命令行匹配 `python` 链（`bot.py` / `src.runtime` / `runner_main`）并终止 |
| `guardian.py` | 每 3 分钟探测 `:8001`（WebUI）与 `:8100`（embedding），连续失败自动拉起；**没有任何计划任务兜底，需自行加入开机自启** |
| `embedding_server.py` | 本地 embedding 服务（fastembed + Qwen3-Embedding-0.6B，`127.0.0.1:8100`） |
| `requirements-embedding.txt` | embedding 服务的依赖 |
| `hidden_run.vbs` | 通用隐藏控制台启动器（WScript.Shell.Run 0, False） |

## 启动顺序

1. `embedding_server.py`（`:8100`，首次启动会下载 ONNX 模型）
2. MaiBot（`:8001`）
3. `guardian.py`

## 注意

- `start_maibot*.bat` 里的 `EULA_AGREE` / `PRIVACY_AGREE` 是上游 EULA 的接受哈希，**换成你接受上游协议后你自己的值**。
- 杀进程**不要用路径过滤**（`Path -like '*deepsleep*'` 会把 embedding 服务和 guardian 一起杀掉），只按命令行匹配 `bot.py` / `src.runtime` / `runner_main`。
- `.bat` 必须是 CRLF 行尾（仓库已用 `.gitattributes` 强制）；含中文的 bat 需 `chcp 65001`。
- 端口一览：`8001` WebUI/主进程、`8100` embedding；其余模型渠道是外网 HTTPS。
