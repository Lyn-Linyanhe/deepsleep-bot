# deepsleep 记忆系统设计文档

> 一套运行在 QQ 聊天机器人上的多层长期记忆系统（基于 MaiBot v1.3.18 / A_Memorix）。
> 本文档描述其**存储结构、写入链路、检索机制、维护策略**，以及实测发现的问题。
> 数据采样时间：2026-10-29。

---

## 一、设计目标

聊天机器人要"像真人"，核心难点不是回复文本，而是**记得住**。传统做法是把聊天记录丢进上下文窗口，但有两个硬伤：

1. **窗口有限**：上下文只有几十条，再往前就忘了；
2. **成本线性增长**：每多带一条历史，每次请求都要重新付一遍 token 钱。

所以这套系统的设计目标是：

- **无限记忆**：原始消息可以无限积累，靠分层压缩保持可用；
- **低成本召回**：不是把所有记忆塞进 prompt，而是按需检索少量相关条目；
- **可遗忘**：不重要的记忆应该自然衰减，避免噪声淹没信号；
- **跨会话**：群里学到的东西，私聊里也能用上。

---

## 二、整体架构

```
                    ┌──────────────────────────────┐
   QQ 消息  ────────►│  mai_messages（原始记录）      │
                    └──────────────┬───────────────┘
                                   │ 每 36 条触发一次摘要
                                   ▼
   ┌───────────────────────────────────────────────────────┐
   │              记忆本体（metadata.db）                    │
   │                                                       │
   │  ① paragraphs   段落        压缩后的叙述文本             │
   │  ② entities     实体        人/物节点                   │
   │     relations   关系        有向边 + 生命周期            │
   │  ③ fact_claims  原子事实    带作用域、权威性、变更历史     │
   │  ④ episodes     情节        "那段时间发生了什么"          │
   │  ⑤ person_profile 人物画像   结构化档案                  │
   │  ⑥ image_*      图片记忆    原图 + 描述 + 相似检索        │
   └───────────────────────────────────────────────────────┘
                                   │
                    三路并行检索 + 加权融合
                                   ▼
   ┌───────────────────────────────────────────────────────┐
   │  稠密向量(0.65) + BM25稀疏(0.20) + 图关系PPR(0.15)       │
   │         → 阈值过滤 → top 10                            │
   └───────────────────────────────────────────────────────┘
                                   │
                    四条注入通道（按需，不是全塞）
                                   ▼
                          进入 LLM prompt
```

---

## 三、存储层

### 3.1 物理位置

| 路径 | 内容 | 实测规模 |
|---|---|---|
| `data/MaiBot.db` → `mai_messages` | 原始聊天记录（一切的地基） | 13,467 条 |
| `data/a-memorix/metadata/metadata.db` | 记忆本体（六层结构） | 12.7 MB |
| `data/a-memorix/vectors/spaces/<hash>/` | Faiss 向量索引（int8 量化，1024 维） | 段落 424 / 图 292 |
| `data/a-memorix/images/assets/` | 图片原文件 | 540 个 |

### 3.2 第 1 层：段落（paragraphs）

**最主要的记忆单元**。每条是一段压缩后的第三人称叙述，不是原话。平均 73 个字符。

真实样例：

```
vasilios称使用 1.3e token 花费四块多。
vasilios表示自己使用 API 是用于开发，认为只跑 bot 纯属玩具、很无聊。
天羽夕莉表示自己也做开发，认为 bot 与开发不冲突；
称用官方 API 跑 bot 消耗会很大，平时只敢挂 OCG。
天羽夕莉认为非订阅状态下缓存命中率一旦下降，费用会很贵。
```

关键字段：

| 字段 | 用途 |
|---|---|
| `hash` | 内容指纹，用于去重 |
| `content` | 叙述文本 |
| `metadata` | JSON：`chat_id`、`evidence_message_ids`、去重状态 |
| `source` | 来源（`chat_summary:<会话>` / `person_fact:<人物>`） |
| `event_time_start/end` | 事件时间（不是写入时间） |
| `access_count` / `last_accessed` | 访问统计（**见 §七 问题 1**） |
| `is_deleted` / `expires_at` | 软删除与过期 |

**写入来源实测分布**：

| 来源类型 | 条数 | 说明 |
|---|---|---|
| `chat_summary:*` | ~269 | 聊天摘要写回，主体 |
| `person_fact:*` | ~155 | 人物事实（短，约 1-6 字） |

### 3.3 第 2 层：实体关系图

从段落中抽取实体与关系，构成一张可做图检索的网。

```
(用户A) --未领到--> API额度        confidence 1.0
(用户B) --评价顺手--> terra        confidence 1.0
```

每个关系带**完整生命周期字段**：

```
retention_strength      记忆强度（会衰减）
reinforcement_count     被强化的次数
last_accessed           上次被检索到的时间
is_pinned / protected_until   手动保护
is_inactive / inactive_reason 失效标记
```

这是"遗忘机制"的载体：强度按半衰期衰减，被访问则强化。

### 3.4 第 3 层：原子事实（fact_claims）

比段落更细粒度、更结构化的知识，支持**冲突替换**和**变更追踪**。

| 字段 | 含义 | 实例 |
|---|---|---|
| `scope_type` / `scope_id` | 事实归属范围 | `chat` / `<会话哈希>` |
| `fact_key` | 事实标识 | `statement:<hash>:<hash>` |
| `value_text` | 内容 | "用户A通过 Google Play 订阅，但一直没领到 API 额度。" |
| `value_normalized` | 归一化形式（小写、去标点） | 用于去重匹配 |
| `stability` | 稳定性 | `stable` / `volatile` |
| `authority` | 权威来源 | `summary_derived` / `user_supported` |
| `profile_section` | 归到画像哪一节 | `stable_facts` |
| `conflict_group` | 冲突组 | 新事实可覆盖同组旧事实 |

配套两张表：
- `fact_evidence`（975 条）：每条事实的证据来源
- `fact_transitions`（996 条）：**事实的变更历史**（A 原来是 X，后来变成 Y）

### 3.5 第 4 层：情节（episodes）

把一段时间窗内的记忆组织成"故事"，用于回答"那几天发生了什么"。

```
标题:   API开发成本与订阅额度讨论
摘要:   vasilios称使用1.3e token花费四块多，表示API主要用于开发…
参与者: ["vasilios", "天羽夕莉", "(# ` n´ )"]
关键词: ["API开发","token成本","bot","dsf","terra","缓存命中率","DeepSeek"]
时间:   精确到分钟，置信度 0.45
证据:   指向 paragraph hash
```

### 3.6 第 5 层：人物画像（person_profile）

为每个活跃用户生成结构化档案，Markdown 格式：

```markdown
# 人物画像
人物ID: <hash>
主称呼: 用户A
别名: [...]

## 身份设定
## 关系设定
- 用户A 未领到 API 额度。
## 稳定了解
## 相处偏好
```

**别名系统**（这是关键设计）：`person_identity_aliases` 328 条 + `relation_hash_aliases` 237 条，
专门解决"同一个人改了昵称/群名片"的实体消歧问题。

### 3.7 第 6 层：图片记忆

```
原图 → 存盘（assets）→ 生成观察描述 → 图片 embedding → 相似检索
                                                    ↓
                                    与对话记忆建立 link（48 条）
```

---

## 四、写入链路

### 4.1 触发时机

| 触发条件 | 配置项 | 当前值 |
|---|---|---|
| 聊天摘要写回 | `chat_summary_writeback_message_threshold` | **每 36 条消息** |
| 人物画像刷新 | `refresh_interval_minutes` | 30 分钟，仅 72h 内活跃者 |
| 情节生成 | `source_poll_interval_seconds` | 每秒轮询，24h 时间窗 |
| 向量回填 | `paragraph_vector_backfill` | 每 60 秒，批量 64 |
| 自动落盘 | `auto_save_interval_minutes` | 5 分钟 |

### 4.2 写一条记忆的完整流程

```
1. 消息进入 mai_messages（原始记录，永不删）
2. 累计到 36 条 → 调 LLM 生成摘要
3. 摘要 → 拆成多段叙述 → 每段算 hash 去重
4. 去重通过 → 写入 paragraphs
5. 同步抽取：实体 / 关系 / 原子事实
6. 计算 embedding → 写入 Faiss（int8 量化）
7. 更新 FTS5 全文索引（jieba 分词）
8. 触发人物画像刷新队列
9. 若段落可归入某情节 → 更新 episodes
```

### 4.3 去重机制

三重去重：

1. **内容 hash**：完全相同直接跳过
2. **`dedup_fact_texts`**：段落内的原子事实级去重
3. **`value_normalized`**：事实归一化后比对（大小写、标点、空格）

---

## 五、检索机制

### 5.1 三路并行检索

```
查询文本
  │
  ├─► 【稠密向量】Faiss, 1024 维, int8 SQ8 量化   权重 0.65
  │     语义相似，能召回"说的不是同一个词但意思相近"
  │
  ├─► 【稀疏检索】SQLite FTS5 + jieba 分词，BM25   权重 0.20
  │     关键词精确命中，兜住向量召回不到的专有名词
  │
  └─► 【图关系】Personalized PageRank（α=0.85）    权重 0.15
        沿实体关系扩展，能召回"间接相关"
              ↓
      weighted_rrf 融合（k=60）
              ↓
      阈值过滤：min_threshold=0.29 / 百分位=75 / min_results=4
              ↓
      最终返回 top_k_final = 10
```

### 5.2 双向量池

配置 `vector_pools.mode = "dual"`，两套池子分别负责不同粒度：

| 池 | top_k | 可展开 | 适用场景 |
|---|---|---|---|
| 段落池 | 20 | — | 事实性回忆 |
| 图池 | 40 | 段落 80 / 关系 5 / 实体 8 | 关系型、多跳查询 |

且针对**关系型意图**单独调权（语义 0.45 / 稀疏 0.15 / **图 0.4**），
因为这些查询靠向量反而召回不好。

### 5.3 记忆如何进入对话（四条通道）

**关键设计：不是把所有记忆塞进 prompt，而是分通道按需注入。**

| 通道 | 触发时机 | 注入量 |
|---|---|---|
| **启发式记忆** | 每 30 条新消息且距上次 ≥180 秒 | 窗口 20 条内取 5 条，≤900 字 |
| **人物画像** | 每轮对话按需 | 最多 3 份 |
| **图片记忆** | 有图片相关上下文时 | — |
| **主动查询工具** | 她主动调用 `query_memory` | 默认 5 条 |

### 5.4 跨会话共享

这是"她记得你"的核心：

```toml
heuristic_memory_group_to_private_enabled = true   # 群里学的 → 私聊能用
heuristic_memory_private_to_group_enabled = true   # 私聊说的 → 群里能用
global_memory_sharing_enabled = true
```

外加 `shared_memory_groups` 可配置更细的共享分组。

---

## 六、维护与遗忘

### 6.1 记忆衰减

```
half_life_hours      = 24     记忆强度半衰期
prune_threshold      = 0.1    低于此值剪除
access_reinforcement = 0.05   每次被检索到强度 +
explicit_reinforce   = 0.5    显式强化（用户纠正等）
freeze_duration      = 24     新建记忆冻结期，期内不衰减
```

设计意图：**常用的记忆越来越牢，不用的自然消失**。

### 6.2 定期任务

| 任务 | 频率 | 说明 |
|---|---|---|
| 摘要写回 | 每 36 条消息 | 增量写入 |
| 画像刷新 | 30 分钟 | 队列化，避免阻塞 |
| 情节生成 | 秒级轮询 | 从段落聚合 |
| 向量回填 | 60 秒 | 补齐缺失向量 |
| 自动落盘 | 5 分钟 | SQLite WAL checkpoint |

### 6.3 视觉/图片维护

```
清理孤立图片 → 压缩过期缓存 → 重试失败的识图任务（最多 5 次）
```

---

## 七、实测健康度与已知问题

采样于 2026-10-29，样本为实际运行 4 天的数据。

### 7.1 运行正常的

| 检查项 | 结果 |
|---|---|
| 向量覆盖率 | **100%**（段落 424 = Faiss ntotal 424；实体 292 = graph 292） |
| 三路检索 | 全部启用（向量 + FTS5 + PPR） |
| 去重机制 | 运行中，`dedup_phase: complete` |
| 嵌入服务 | 在线，未处于降级态 |
| 数据完整性 | 无孤儿记录，无损坏 |

### 7.2 发现的问题

**① `access_count` 全为 0（424/424）**

回忆行为从未被回写统计。后果：遗忘机制里"被访问则强化"这条路径实际上没有数据，
系统无法学习"哪些记忆重要"。这是**当前最值得修的问题**。

**② `word_count` 字段是坏数据**

平均显示 3-6 字，但实际内容有 200+ 字。字段未被正确计算（写入时漏了赋值或用了错误的分词结果）。
影响：任何基于字数阈值的过滤/统计都会失效。

**③ 单会话占比过高**

最大单群占 **38%** 的段落（159/424）。记忆分布严重倾斜，小众会话的细节容易被忽略。

**④ 图片向量未建立**

`image_assets` 有 540 条，但 `image_embeddings` 为 **0 行**。
图片记忆目前只有文字描述可用，没有相似图检索能力。
（配置 `min_train_threshold = 40`，可能因条件未满足而未触发训练）

**⑤ 纠错闭环关闭**

`feedback_correction_enabled = false`。假设她记错了，系统不会自动用后续对话来修正。

---

## 八、可调参数速查

### 影响成本的

```toml
# 摘要写入频率——越低越省，但记忆粒度越粗
chat_summary_writeback_message_threshold = 36

# 注入量——直接影响每次请求的 prompt 大小
heuristic_memory_recall_limit = 5
heuristic_memory_recall_max_chars = 900
person_profile_injection_max_profiles = 3

# 检索量
retrieval.top_k_final = 10
retrieval.top_k_paragraphs = 20
```

### 影响质量的

```toml
# 检索融合权重（要调召回质量就动这里）
retrieval.fusion.vector_weight = 0.7
retrieval.fusion.bm25_weight = 0.3
retrieval.vector_pools.relation_intent.graph_weight = 0.4

# 阈值：高了漏召回，低了噪声多
threshold.min_threshold = 0.29
threshold.percentile = 75
threshold.min_results = 4

# 遗忘速度
memory.half_life_hours = 24
memory.prune_threshold = 0.1
```

### 影响新鲜度的

```toml
person_profile.refresh_interval_minutes = 30
episode.source_time_window_hours = 24.0
embedding.paragraph_vector_backfill.interval_seconds = 60
```

---

## 九、这套设计的取舍

### 做得对的地方

1. **分层压缩**：原始消息永不删，各层按不同粒度压缩，兼顾"记得全"和"查得快"。
2. **多路召回**：单靠向量会漏专有名词，单靠关键词会漏语义相似，三路融合互补。
3. **按需注入**：不把所有记忆塞进 prompt，而是检索 top 10，成本可控。
4. **生命周期管理**：关系带衰减强度，避免记忆库无限膨胀成噪声。
5. **别名消歧**：专门处理改名问题，这在多人群聊里是必需项。
6. **事实可变更**：`fact_transitions` 记录"原来是 X 后来变 Y"，而不是简单覆盖。

### 代价与局限

1. **写入有延迟**：36 条消息才摘要一次，刚聊完的事情不会立刻被"记住"。
2. **摘要会失真**：LLM 压缩必然丢细节，长尾信息会消失。
3. **依赖 embedding 服务**：本地服务挂掉会退化成只有关键词检索（有降级机制但质量下降）。
4. **遗忘不可控**：半衰期机制是启发式的，"重要但不常提"的记忆可能被误删。
5. **单点集中**：所有记忆在一个 SQLite 里，损坏即全失（需靠备份）。

---

## 十、延伸：如何评估这类系统

如果你在做类似设计，建议关注这几个指标：

| 指标 | 含义 | 健康值参考 |
|---|---|---|
| 向量覆盖率 | 有向量的记忆 / 总记忆 | 应接近 100% |
| 检索命中率 | 检索返回非空的比例 | 应 > 90% |
| 记忆复用率 | `access_count > 0` 的比例 | 应 > 50% |
| 记忆增速 | 每天新增段落数 | 突增说明去重失效 |
| 单会话集中度 | 最大会话占比 | 应 < 20% |
| 注入占比 | 记忆占 prompt 的 token 比例 | 10-30% 较合理 |

本系统实测：前两项 100% ✅，第三项 **0%** ❌，第五项 38% ⚠️。
