# 第三方与许可说明

## MaiBot

本仓库的补丁与插件基于 [MaiBot](https://github.com/Mai-with-u/MaiBot)（GPL-3.0）。
MaiBot 框架本体不包含在本仓库，使用请遵循其 GPL-3.0 许可。

## 狗头军师

`plugins/deepsleep_counselor` 与 `plugins/deepsleep_speech` 的设计源自
[powerycy/goutoujunshi](https://github.com/powerycy/goutoujunshi)（PolyForm Noncommercial 1.0.0）。

按该许可要求，我们**未随仓库发布任何知识正文**（知识摘要与提示卡均未包含），仅保留程序结构。
如需完整能力，请从上游获取知识库并自行转换格式（见各插件的 README）。

## SnowLuma

QQ 聊天网关 SnowLuma 为独立第三方项目，不包含在本仓库。

## MaiTrace

`patches/0002-maitrace-audit-fixes.patch` 基于 MaiTrace v6.2.4，遵循其 **AGPL-3.0-only** 许可，完整许可见 `patches/LICENSE-MaiTrace-AGPL-3.0.txt`。它不受本仓库默认 GPL-3.0 声明替代。应用补丁时应保留上游 NOTICE 与 LICENSES 中的既有组件声明。

## 个人数据

本仓库不包含任何聊天记录、记忆库、日志、人设图片或真实账号信息。
所有 QQ 号 / 群号在发布前已替换为占位符。
