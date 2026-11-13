# tests

7 组离线单元测试（共 **112 项**），验证记忆、回复安全与插件相关的关键逻辑。全部基于 **AST 提取源码函数后在内存里跑**：不联网、不写生产数据库、不调用模型。

## 运行前提

- 需要一个已部署的工作区：`MaiBot` 源码（v1.3.18 基线 + 已应用 `patches/0001-maibot-deepsleep-customizations.patch`）、本仓库三个插件已放入 `MaiBot/plugins/`
- 用环境变量指向工作区根目录（其下应有 `MaiBot/`）

```bash
# Windows
set DEEPSLEEP_WORKSPACE=D:\deepsleep
python tests/test_speech_injector.py

# Linux / macOS
DEEPSLEEP_WORKSPACE=/path/to/deepsleep python tests/test_speech_injector.py
```

## 测试清单

| 文件 | 项数 | 覆盖 |
|---|---|---|
| `test_counselor_tool.py` | 20 | 主题摘要加载、关键词打分、危机信号置顶、注入上限 |
| `test_feedback.py` | 8 | 表达反馈闭环（被怼 −1 / 表情回应 +1） |
| `test_memory_dedup.py` | 16 | 注入侧 bigram 近重复抑制、占位过滤防误杀、写侧占位拦截 |
| `test_recency_decay.py` | 7 | 段落时效衰减：stamp 时序、score×factor 重排、平局稳定 |
| `test_reply_tool_leak.py` | 6 | 工具协议文本泄露三阶段拦截 |
| `test_speech_injector.py` | 44 | 提示卡匹配、会话分频、冷却、指定用户通道、Planner 注入 |
| `test_today_usage.py` | 11 | 费用查询工具：鉴权、群过滤、金额口径、图片配额 |

> 测试里的 QQ 号/群名均为占位或虚构值；主人 QQ 从被测源码的 `_OWNER_QQ` 常量读取，不硬编码。
