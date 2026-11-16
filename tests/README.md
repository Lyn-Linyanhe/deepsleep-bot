# 测试

当前修复版入口为 `test_audit_repairs.py`，共 **19 项离线回归**。通过 AST 隔离加载实际源码函数，使用虚构消息、假接口与工作区临时 SQLite；不联网、不调用模型、不访问生产数据库。

## 运行

需要已应用本仓库补丁的 MaiBot 与 MaiTrace，以及工作区根目录的 guardian.py、embedding_server/app.py。脚本会在工作区 out 下创建并清理测试临时目录。

```bat
set DEEPSLEEP_WORKSPACE=D:\deepsleep
python tests/test_audit_repairs.py
```

覆盖身份快照与插件鉴权、静默历史、跨事件循环限流及取消、热调上限、配图返回结构、空间静默、记忆过滤与计数失败、反馈伪造与重放、guardian冷却、embedding真实并发模拟、单字打字速度。

这些是隔离逻辑测试，不等同于完整 SDK、QQ 或模型集成测试。生产环境未安装 pytest，因此未运行完整上游测试套件。

## 历史基线

其余 `test_*.py` 是公开仓提交 `b0d07b8` 所保留的旧补丁测试，历史统计为7组112项。这些测试未随新接口全部迁移，不代表当前版本通过，也不要用目录通配运行来作为新版验收。

其中 `test_feedback.py` 的旧夹具缺少真实消息身份及结构化通知；`test_memory_dedup.py` 仍要求已经移除的宽松 bigram 合并。这两组旧预期与安全修复不兼容，应参考新回归入口。其他历史测试本次也没有重新验收。
