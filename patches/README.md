# 补丁与基线

`0001-maibot-deepsleep-customizations.patch` 是相对 MaiBot **v1.3.18** 的累计源码补丁，包含原有定制及 2026-11-17 审计修复，不是叠加在旧版 0001 上的增量补丁。

在干净的 MaiBot v1.3.18 工作树中：

```bash
git apply --check /path/to/deepsleep-bot/patches/0001-maibot-deepsleep-customizations.patch
git apply /path/to/deepsleep-bot/patches/0001-maibot-deepsleep-customizations.patch
```

再将 `new-files/src/learners/expression_feedback.py` 放到 `MaiBot/src/learners/`。

`0002-maitrace-audit-fixes.patch` 单独应用于 MaiTrace **v6.2.4 / commit 4705c253706c5a936633db905fff6580ade3ba74**，在 MaiTrace 仓库根目录执行 `git apply --check` 后应用。它包含生活状态、睡眠消息保留、发送门禁、空间配图与静默的本地改动；必须同时使用上述 MaiBot 补丁，`store_without_reply` 宿主契约不能单独部署。

MaiTrace 补丁遵循上游 **AGPL-3.0-only**，许可全文见 `LICENSE-MaiTrace-AGPL-3.0.txt`；上游既有 MIT 组件声明应随上游完整分发保留。MaiBot 累计补丁遵循其 GPL-3.0 许可。

所有真实账号已替换为占位符；请在本地配置权限前核对账号，不要照搬占位账号。第三方适配器的其他定制见 `notes-snowluma-adapter.md`。

## 本次修复

- 当前真实消息批次的调用者鉴权；插件身份覆盖保护。
- 静默消息保留及醒后历史恢复；部分发送成功状态保护。
- 跨事件循环模型限流、热更新存量计数与取消释放。
- 段落最终返回后计数、摘要去重选用就绪段落池。
- 保守占位过滤、不同事实不按 bigram 合并、私聊摘要隔离。
- 表达反馈结构化通知校验、会话绑定与重复反馈抑制。

验证和限制见 `../docs/audit-update-20261117.md`。升级上游时需重新核对应用位置和接口，不保证跨版本直接应用。
