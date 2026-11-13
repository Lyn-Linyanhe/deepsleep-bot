# config-templates

从实际运行配置脱敏得到的模板。复制到 `MaiBot/config/` 后需要自己填：

## model_config.toml

- 所有 `api_key = "<YOUR_API_KEY>"` 换成你的密钥；`base_url` 已替换为 `example.com` 占位，按你的服务商填写
- `[model_task_config.*]` 里的模型链（planner / replyer / memory / memory_episode / learner / vlm / emoji …）按你的 provider 名调整
- 本项目的渠道分层：在线任务走「付费弹性 → 免费」、后台任务走「免费 → 付费弹性」，避免免费额度被后台任务占满

## bot_config.toml

| 占位符 | 含义 |
|---|---|
| `100000000` | 主人 QQ（日报接收人、费用查询鉴权） |
| `100000001` | 机器人自己的 QQ |
| `2000000xx` / `3000000xx` | 群号占位 |
| `群A`~`群G` | 群名占位（`talk_value_rules` 注释里） |

- `personality` / `behavior_style` / `reply_style` 是核心人格与说话分层，可按自己的角色重写；改完保存即热重载，日志确认 `配置热重载完成`
- 注意**分层**：Planner 读 `behavior_style`，Replyer 读 `reply_style`，人格两处都读——态度类改动必须同时落在人格与行为层

## 其它

- `adapter_policy.toml` 未提供（内容为会话级策略，因部署而异）
- `webui.json`、数据库、日志等运行时文件永远不要提交
