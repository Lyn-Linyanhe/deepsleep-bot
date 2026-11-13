# 自研插件

三个插件都跑在 MaiBot 插件运行时（`MaiBot/plugins/`），每个目录包含 `_manifest.json`、`plugin.py`、`config.toml`。

| 插件 | 说明 |
|---|---|
| `deepsleep_daily_report` | 每日用量日报 + 对话内费用查询。数据源是 MaiBot 自己的 `llm_usage` 明细表；查询工具的鉴权走宿主注入的上下文（模型无法伪造），群里只确认已私发、不公开金额 |
| `deepsleep_counselor` | 关系/社交知识按需检索。`relationship_counsel` 工具（当前停用）；危机信号（家暴/自杀/跟踪/诈骗）强制置顶安全指引 |
| `deepsleep_speech` | replyer/planner 请求前注入"说话提示"卡；支持按会话类型分频与指定用户通道（当前停用） |

## 插件最小接口（踩坑）

MaiBot 插件必须满足三件套，缺一个都加载失败：

1. `MaiBotPlugin` 子类
2. 模块级 `create_plugin()` 工厂函数
3. `on_config_update(scope, config_data, version)` 方法

改完 `plugin.py` 后无需重启整机：调用 `POST /api/webui/plugins/config/{plugin_id}/toggle` 关再开即可热加载代码。

## 知识库说明

- `deepsleep_counselor/knowledge/`：请自建主题摘要（格式见目录内 README）
- `deepsleep_speech/cards/`：请自建提示卡（格式见目录内 README）
