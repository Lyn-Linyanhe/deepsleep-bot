# patches

相对 [MaiBot](https://github.com/Mai-with-u/MaiBot) 上游（GPL-3.0）的源码改动，导出为单个补丁：

```bash
git apply patches/0001-maibot-deepsleep-customizations.patch
```

新文件（未被上游跟踪）需手动放置：

```bash
cp patches/new-files/src/learners/expression_feedback.py  MaiBot/src/learners/
```

## 覆盖内容

| 领域 | 文件 |
|---|---|
| 记忆注入 | `maisaka/memory/heuristic_injector.py`、`person_profile.py`、`image_injector.py` |
| 检索与衰减 | `A_memorix/core/retrieval/dual_path.py`、`utils/search_execution_service.py`、`runtime/services/memory_search_service.py`、`storage/metadata_store.py` |
| 写入侧 | `A_memorix/core/utils/summary_importer.py`、`person_profile_service.py` |
| 模型链与配置 | `config/model_configs.py`、`config/official_configs.py`、`services/memory_flow_service.py`、`llm_models/utils_model.py` |
| 表达学习 | `learners/jargon_miner.py`、`learners/expression_feedback.py`（新）、`chat/replyer/maisaka_expression_selector.py` |
| 回复安全 | `maisaka/builtin_tool/reply.py` |
| 插件运行时 | `plugin_runtime/component_query.py` |

补丁基于 MaiBot v1.3.18 时代的代码，升级上游时可能需要 rebase。
