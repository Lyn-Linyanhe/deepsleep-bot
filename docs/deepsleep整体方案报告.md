# deepsleep 项目整体方案报告

**核查时间：2026-10-29**  
**范围：** `D:\deepsleep` 工作区、MaiBot 当前源码/配置、SnowLuma、插件、A_Memorix、embedding 服务与 guardian。  
**核查方式：** 读取当前文件和运行日志，检查当前配置、数据库只读统计及插件列表；没有修改生产配置、模型或数据库。  
**版本口径：** 当前运行日志确认 MaiBot `1.3.18`、插件 SDK `2.19.0`、SnowLuma `2.2.0`；MaiBot Git 工作树为 `v1.3.18-1-gb94a3e72a-dirty`，存在两处未提交的记忆代码改动。根部署文档开头保留了早期部署状态，和当前运行状态不一致；本文以现场代码、配置与日志为准。

## 1. 项目目标

deepsleep 是基于 MaiBot 的拟人化 QQ 聊天机器人。方案重点不是单纯“接一个大模型”，而是把人格、群聊/私聊决策、工具、长期记忆、QQ 协议、生活状态和运维组合成一个持续运行的系统。

核心目标：

- 在 QQ 群聊中读空气、择机发言；私聊中优先回应。
- 用统一人格和 Planner/Replyer 两阶段模型实现行动决策与自然语言回复。
- 通过工具扩展表情、图库自拍、生图、语音、联网、记忆、主动私聊和 QQ 空间等能力。
- 以 A_Memorix 维护跨会话的长期记忆，并通过本地 embedding 提供语义检索。
- 用 SnowLuma 承担 QQ 协议和账号注入；用日志、日报、guardian 管理长期运行。

当前人格是**全局一套**，不是每个群各有一套。群级差异主要来自 `talk_value_rules` 和可选的 `chat_prompts`。当前会话配置中的 `chat_prompts=[]`，A_Memorix 普通记忆查询允许全局共享；相关配置见 `MaiBot/config/bot_config.toml` 的 `[personality]`、`[chat.reply_style]`、`[a_memorix]`。

## 2. 部署拓扑

```text
QQ 客户端
  ↕ DLL 注入 / QQ 协议
SnowLuma 2.2.0（Node，OneBot）
  ↕ WebSocket 127.0.0.1:3001；HTTP 127.0.0.1:3000
MaiBot SnowLuma Adapter（插件 Runner）
  ↕ Platform IO / MessageGateway / Host IPC
MaiBot 1.3.18 主进程（8000 消息服务、8001 WebUI）
  ├─ Maisaka：会话 Runtime、Planner、工具执行、Replyer
  ├─ 插件 Host + builtin / third-party Runner 子进程
  ├─ A_Memorix：SQLite 元数据、向量库、图关系
  ├─ LLM Provider：geili / geili2 / tokendance / luna
  └─ 本地 embedding HTTP 服务：127.0.0.1:8100

guardian.py：每 180 秒探测 8001 和 8100；连续两次失败后拉起
```

主要代码和目录：

- `MaiBot/src/main.py`：应用初始化、后台任务和组件关闭顺序。
- `MaiBot/src/runtime.py`、`MaiBot/src/common/process_runner.py`：Worker/Runner 生命周期。
- `MaiBot/src/platform_io/`：消息收发的平台驱动层。
- `MaiBot/src/chat/message_receive/`：消息入口、命令拦截、会话注册。
- `MaiBot/src/maisaka/`：按聊天流运行的 Planner/ReasoningEngine/工具上下文。
- `MaiBot/src/plugin_runtime/` 和 `MaiBot/.venv/Lib/site-packages/maibot_sdk/`：插件 Host、Runner、SDK 能力。
- `MaiBot/plugins/`：OAImage、MaiTrace、SnowLuma Adapter、AnySearch、Edge TTS、表情选择、SenseVoice 等插件。
- `MaiBot/src/A_memorix/`：长期记忆检索、写入、向量与关系图。
- `embedding_server/app.py`：本机 Qwen embedding HTTP 端点。
- `SnowLuma/`：独立 Node QQ 协议侧车。

## 3. 一条消息如何变成回复

```text
QQ 入站事件
→ SnowLuma 解码 OneBot 事件
→ SnowLuma Adapter WebSocket / MessageGateway
→ MaiBot SessionMessage 与 session_id
→ ChatBot 前置处理、命令/过滤、入库
→ 对应群或私聊 Maisaka Runtime
→ 防抖、回复门控、上下文选择与动态注入
→ Planner 决定“做什么”并选择工具
→ Tool Registry 执行工具
→ 需要可见文字时调用 reply 工具
→ Replyer 决定“具体怎么说”
→ send_service / Platform IO
→ SnowLuma 发到 QQ，收集发送结果并写回消息历史
```

### 3.1 入站与会话

SnowLuma Adapter 将 OneBot 事件整理成 MaiBot 统一消息，主程序按平台、账号、群/用户、scope 计算会话标识。群和私聊分别有会话流，消息入库后进入对应 Maisaka Runtime。重要入口：`src/chat/message_receive/bot.py`、`src/chat/heart_flow/heartflow_message_processor.py`、`plugins/snowluma_adapter/runtime/router.py`、`src/platform_io/manager.py`。

### 3.2 决策与回复

- **Planner** 读取人格、behavior_style、历史和工具定义，决定回复、等待、查记忆、调插件或不行动。
- **ReasoningEngine** 执行 Planner 的工具循环，持有当前轮次状态；它是编排逻辑，不是另一个模型。
- **Replyer** 在 `reply` 工具中产生面向人的自然语言、引用、附件与表情组合。
- 当前 `passive_replyer=false`，因此普通回复采用 Planner 调 `reply`、Replyer 生成正文的方式。
- Planner/Replyer 的具体模型路由在 `model_config.toml`，候选顺序策略是 `sequential`：主模型成功时优先使用首项，失败并耗尽重试后才切备用。

当前模型任务路由：

| 任务 | 候选路由 | 作用 |
|---|---|---|
| `planner` | geili-chat → geili2-chat → geili-think → tokensance-chat | 决策和工具选择 |
| `replyer` | geili-chat → geili2-chat → tokensance-chat | 可见文字回复 |
| `memory` / `utils` / `learner` / `fast_model` / `emoji` | geili-chat → geili2-chat | 记忆、整理、学习、小任务 |
| `vlm` | luna-vision → geili-vision → geili2-vision | 图片识别与文字描述 |
| `embedding` | local-bge-m3 → 本地 Qwen3-Embedding-0.6B | 文本向量 |
| `image_embedding` | 空 | 当前没有为图片记忆配置向量模型 |

近 24 小时 `llm_usage` 中已出现 36 次 `luna-vision` 调用，说明 gpt-6-luna 已被 VLM 路由实际调用；同一时间窗仍有 geili 和 geili2 vision fallback 调用。

`geili-chat`、`geili-think`、`geili-vision` 是同一上游 DeepSeek 模型、同一 geili key 的不同请求参数配置；think 通道开启 reasoning，vision 通道分配给图片理解任务。`geili2-*` 是第二把 key，按当前配置为 0.5 倍档；geili 主 key 是 0.25 倍，tokendance 是 1 倍。`luna-vision` 使用用户给的 gpt-6-luna 路由和 1:1 美元/人民币换算，价格配置为输入 0.015、缓存读 0.0015、输出 0.075（价格区间按用户说明采用 <272k 档）。

### 3.3 群聊与私聊行为

- 私聊全局暂停为 `false`；暂停规则为空。
- 私聊进入普通消息处理后不使用群聊 `talk_value` 概率门控；动态 gate 中明确私聊直接 trigger Planner。
- 群聊有全局频率和 per-group `talk_value_rules`，@/提及、急需帮助、动态 gate 会影响是否启动 Planner。
- 群聊和私聊都使用防抖设置；目前 `natural`，新消息可能先短暂合批。
- 不行动后的 backoff 会延后检查，但新消息达到 bypass 条数可提前重新处理。

### 3.4 图片理解和图片发送

识图有“直接看图”和“缓存描述”两层路径，不能简化成只有文字转写：

1. 有待识别的图片时，`vlm` 任务可接收原图和识别提示并生成描述。
2. 识别结果写入图片记录；历史刷新器会将占位 `[图片，识别中.....]` 改成 `[图片：描述]`，后续上下文可复用缓存文字，避免重复识别。
3. 如果当前请求的模型配置支持视觉且视觉上下文开启，图片组件也可直接转成多模态内容给 Planner/Replyer；是否直接传图取决于当前 request kind、模型 visual 配置及图片限制，不应断言所有聊天都只收到文字。

图片发送分两路：`send_gallery_pic` 从预先生成的 8 张图库照片选择并发送；OAImage 插件 `generate_image` 通过外部 gpt-image-2.5-flare 服务现场生成，日上限 15 张。两条路径分别适合匹配现成场景、或需要新画面的情形。

## 4. 长期记忆方案

A_Memorix 是本项目自己的长期记忆模块，不等同于聊天上下文窗口。当前运行数据位于 `MaiBot/data/a-memorix/`，活动元数据 SQLite 是 `metadata/metadata.db`，活动向量空间由 `vectors/active.json` 指向。

### 4.1 检索

检索流程概括为：

1. 根据调用者身份、聊天范围、时间和人物约束确定可检索范围。
2. 通过本地 Qwen embedding 做段落/实体语义召回。
3. 通过 SQLite FTS5 + jieba 做稀疏词项召回。
4. 通过实体/关系和图证据补充关联信息。
5. 对 semantic / sparse / graph 分数校准融合，再按 stale、隐私范围和知识状态过滤。

配置权重大致为常规查询 semantic 0.65、sparse 0.20、graph 0.15；关系意图查询提高 graph 比重。实际检索会基于证据质量校准权重。

### 4.2 写入

记忆材料来自会话消息、滚动摘要、人物事实、实体关系和图片关联。metadata 先写入 SQLite；向量、图结构及派生索引在后续步骤或后台队列更新。SQLite 与向量/图文件并非一个原子事务，系统通过失败状态、重试和 backfill 补齐。

### 4.3 当前规模与状态

核查时只读统计：437 段落、309 实体、396 关系、200 episodes、564 fact claims。当前 `dual_ready.json` 标记 433 段落向量、307 图向量，关系向量为 0；metadata 与 ready manifest 计数略有差异，运行时会继续有新写入，不能把两者当同一时刻快照。最新日志显示 embedding 已恢复并退出降级态。

当前配置 `global_memory_sharing_enabled=true`，普通记忆默认跨聊天流共享；如需隔离特定私人事实，需要单独核对 scope/filter 实现和 `shared_memory_groups` 策略。

## 5. 插件与能力扩展

MaiBot 插件运行在独立 Runner 子进程，通过 SDK 声明工具、消息网关、事件、Hook，并以 IPC 请求主程序能力。当前启用的 12 个插件：

- **SnowLuma Adapter**：QQ OneBot WebSocket 入站、出站；提供已有联系人主动私聊等适配工具，不会自动加好友。
- **Rabbit-Jia-Er MaiTrace**：日程、生活状态、活动素材、主动私聊、QQ 空间 Routine、聊天注入。
- **OAImage**：写实/插画生图、图片配额和参考图处理。
- **AnySearch**：第三方联网搜索；统一 web_search 服务列表目前为空。
- **Edge TTS**：外部语音合成并经适配器发送语音。
- **SenseVoice STT**：语音识别插件；当前 endpoint 指向 `sensevoice:50000`，是否有外部服务需部署环境确认。
- **Emoji Selector**：按聊天语义挑选表情。
- **Private Humanizer**：私聊回复扩展。
- **Color2333 多平台解析器**：消息内容解析。
- **TAIY Smart Poke**：群聊戳一戳策略和状态管理。
- **deepsleep.daily-report**：每天 0 点统计上一自然日 LLM token/费用和生图张数，私聊给主人。
- `hello_world_plugin` 安装但禁用。

是否可用还取决于插件配置、SDK capability 授权、外部服务端点和模型任务是否有模型。插件“已加载”不等于每个功能路径都已实测可用。

## 6. 主动私聊与生活状态

MaiTrace 的 life 引擎每分钟推进生活状态，并巡检主动私聊。主人 profile 当前 enabled、proactive_enabled、每日上限 3；全局主动私聊开启，最小间隔 120 分钟、主人静默阈值 15 分钟、精力下限 30、主人静默时段 23:00–08:00。发送目标是已存在的 QQ 私聊会话，不负责加好友。

**当前核查时发现的已知故障状态：** life SQLite 中主人 energy=0、mood_valence=-1、sleep_phase=awake、health_status=tired，且没有 pending opportunity；因此主动私聊会被 `private_min_energy=30` 门槛挡住。life engine 的 energy 模型在睡眠段恢复（+8/小时），清醒日程按活动负荷下降；当前状态有能量归零后长期无法主动回升的风险。这个报告只记录问题，不在未经明确安排下擅自改动 life 状态或生产插件逻辑。

此外 MaiTrace 的 Schedule 主动通道 `proactive_streams=[]`，和 Life 私聊 patrol 是不同机制；不要把 `schedule.proactive_streams` 为空误解为 Life 私聊 profile 未启用。

## 7. 主人授权的外接 Agent

AgentService 在 MaiBot 主进程内管理 ACP 队列，按需启动 DSH CLI。当前 `[agent] enabled=true`，连接 `dsh`，工作目录 `D:\deepsleep`；`planner_enabled=false`，因此 DSH 不替代日常 Planner。主人 QQ 账号白名单可见并调用 `agent_talk`，代码会在工具列表和 invoke 两层检查 speaker IDs。

电脑工具配置为 false、files capability 为 false。当前该主机的 DSH/workspace-write 行为边界需区分文件工具沙箱与 shell：AGENTS.md 记录 shell 的工作区外写入边界曾未经验证/可能不生效，因此应按“不能假设 shell 被限制”处理。

## 8. 运维、数据与安全

### 8.1 运行守护

- `guardian.py` 每 180 秒探测 MaiBot 8001、embedding 8100，连续两次失败后尝试拉起；每 3 小时跑 health report 和图片归档。
- MaiBot 本身由 Windows 启动脚本运行；`start_maibot_wrapper.bat` 负责 embedding 检测和启动。
- Windows 注意事项：bat 必须 CRLF；Git Bash 会改写 `/c` 等参数；`Get-NetTCPConnection` 在该机可能挂死，当前以 netstat/socket 探测为准；Python venv 可能表现为 launcher trampoline + 真实解释器子进程，实例数量应看监听端口而非进程行数。
- 最新运行日志确认 1.3.18、DB schema 46、embedding 恢复；当前监听 3001、5099、8000、8001、8100。部署后是否端到端 QQ 回执成功，本次只按日志/端口验证，不代表现场重新发了一条测试消息。

### 8.2 统计和审计

- MaiBot `data/MaiBot.db` 保存聊天、会话、`llm_usage`、插件相关统计等。
- `llm_usage` 是模型 token 和成本账本；日报按自然日统计，并把 OAImage 日配额库张数 × 每张 ¥0.03 计入总支出。
- `health_report.py` 扫描启动日志中的预定义异常并输出 Markdown / JSONL 台账；guardian 是端口级健康检查，不等同于对聊天链路、记忆检索质量或外部 API 的完整监控。
- 图片归档脚本将数据库登记的图片复制到工作区外的 `图片库`，附类别和索引；归档结果和原始图片都属于需要保护的用户数据。

### 8.3 安全与隐私

- WebUI 和本机服务监听回环地址；WebUI 以 Cookie token 验证。
- 模型 API keys、OneBot token、SnowLuma 面板密码等属于敏感凭据，配置中有明文存储。本文不复述具体值。
- `agent_talk` 能把任务交给 DSH；即便电脑工具与 files capability 当前关闭，也要遵守工作区约束，不能把“聊天工具禁用”误当成“DSH shell 没有权限”。
- A_Memorix 默认跨聊天流检索，是隐私设计的明确选择；需把聊天来源/scope过滤当作安全控制，而不是假设群与私聊天然隔离。
- 第三方 QQ 协议接入可能违反平台用户协议，账号存在被限制风险；应把协议端与正式账号的风险单独评估。

## 9. 主要设计取舍

| 选择 | 目的 | 代价/限制 |
|---|---|---|
| Planner 与 Replyer 分开 | Planner 决定行动，Replyer 写自然语言 | 一次正常回复可能有多次模型请求 |
| 按聊天流隔离运行时 | 每个会话有独立历史和状态 | 并发/上下文成本随活跃会话增长 |
| 全局人格 + 单独聊天提示 | 保持人格一致，同时允许群级规则 | 当前 `chat_prompts` 为空，没有群级语气差异 |
| 长期记忆与短期历史并行 | 跨会话记忆，历史仍控制近期上下文 | 记忆召回范围过宽会有隐私风险；向量/metadata 更新非原子 |
| 多供应商顺序兜底 | 上游故障时保可用性 | failover 会更换 key/缓存域并增加成本不确定性 |
| 识图结果缓存成文字 | 后续可复用图片描述，降低重复 VLM 请求 | 图片描述异步完成会改写历史内容，影响 prompt 前缀缓存 |
| 照片图库 + 现场生图 | 匹配已有照片时快发，缺场景再生成 | 图库覆盖有限；现场生成慢且有配额/费用 |
| 本地 embedding + 多路检索 | 保留语义、关键词和图关系召回 | 需要维护模型指纹、向量回填、索引一致性 |

## 10. 当前已知问题与建议排序

### P1：MaiTrace 私聊主动机制会被 life energy 卡死

当前主人 energy=0、门槛要求 >=30；活动段模型在清醒时持续扣减、主要靠睡眠段恢复。建议设计安全上限内的“休息可恢复”机制，并明确 energy 归零时的恢复/重置路径；修复应加离线状态跨日和 sleep/wake 的回归测试。当前报告不修改此行为。

### P1：记忆代码有未提交改动

当前 Git 工作树的 `dual_path.py` 未提交改动会把检索命中写回 `access_count/last_accessed`，用于遗忘/强化统计；`metadata_store.py` 未提交改动将中文段落 `word_count` 从空格分词改为非空白字符计数。它们直接影响记忆核心，应单独测试、审查并形成可回滚提交；本报告刻意保留未提交状态。

### P1：凭据和隐私范围

明文 API keys / OneBot tokens、默认跨聊天流记忆检索、DSH shell 的实际权限均需持续审计；尤其确认 DSH shell 的工作区外访问行为，不能依赖提示词作为唯一安全措施。

### P2：监控是端口/日志级别

guardian 能发现 8001/8100 不通，但不能发现“HTTP 活着、QQ adapter 已断开”“LLM 全部超时”“向量索引滞后”等半故障。建议 health report 增加 adapter heartbeat、关键模型最近成功时间、Memory self-check 与 metadata/vector 差值。

### P2：SenseVoice 外部端点需部署确认

插件配置指向 `sensevoice:50000`；本机启动脚本未见启动对应服务。确认其容器/DNS/远端服务来源，或在系统方案中标为未启用/不可用。

### P2：文档需要建立“当前状态”单一权威段

部署文档前部仍有 v1.3.15 和早期插件/记忆状态，后续章节才记录 v1.3.18。建议维护一页简短当前清单，并将时间线历史明确标为历史记录，避免读者把旧状态当成现状。

## 11. 一句话总结

deepsleep 的方案是：**MaiBot 负责人格化会话与工具决策，SnowLuma 负责 QQ 协议，插件扩展生活/图片/语音/搜索能力，A_Memorix 提供长期记忆，本地 embedding 支撑语义检索，guardian 与日报提供基础运维；最需要继续打磨的是私聊生活能量恢复、记忆写入一致性、隐私隔离和端到端健康监控。**

## 12. 关键文件索引

- 项目约束与身份：[AGENTS.md](AGENTS.md)
- 历史部署与故障日志：[部署说明.md](部署说明.md)
- 主程序配置：[MaiBot/config/bot_config.toml](MaiBot/config/bot_config.toml)、[MaiBot/config/model_config.toml](MaiBot/config/model_config.toml)
- 消息链路：[MaiBot/src/chat/message_receive/bot.py](MaiBot/src/chat/message_receive/bot.py)、[MaiBot/src/maisaka/runtime.py](MaiBot/src/maisaka/runtime.py)、[MaiBot/src/maisaka/reasoning_engine.py](MaiBot/src/maisaka/reasoning_engine.py)
- 记忆实现：[MaiBot/src/A_memorix](MaiBot/src/A_memorix)、活动数据库 `MaiBot/data/a-memorix/metadata/metadata.db`
- 插件与适配器：[MaiBot/plugins](MaiBot/plugins)
- embedding：[embedding_server/app.py](embedding_server/app.py)
- 守护与巡检：[guardian.py](guardian.py)、[health_report.py](health_report.py)
- 当前运行日志：`MaiBot/logs/app_20261029_220700.log.jsonl`
