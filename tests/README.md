# 测试

当前修复版有三个显式入口：

| 入口 | 验证范围 |
|---|---|
| `test_audit_repairs.py` | 19项审计回归：权限、静默历史、跨事件循环限流、空间、过滤、反馈和服务生命周期 |
| `test_memory_optimization.py` | 8项记忆回归：完整预算、最终采用集合、回执范围/时效/单次消费、失效记录、参数透传 |
| `test_loopback_proxy_routing.py` | 代理路由检查：真实客户端构造方法、本地假embedding服务和不可用代理，不调用模型 |

前两项通过AST隔离加载实际源码函数，使用虚构消息、假接口及工作区临时SQLite，不联网、不访问生产数据库。第三项只在本机回环地址启动临时HTTP服务，不连接公网，也不依赖真实模型推理速度。

## 运行

需要已应用本仓库累计补丁的MaiBot、MaiTrace，以及工作区根目录的guardian.py、embedding_server/app.py。审计脚本会在工作区out下创建并清理测试临时目录；代理测试使用MaiBot环境中的openai依赖。

```bat
set DEEPSLEEP_WORKSPACE=D:\deepsleep
python tests/test_audit_repairs.py
python tests/test_memory_optimization.py
python tests/test_loopback_proxy_routing.py
```

这些是隔离逻辑和本地传输测试，不等同于完整SDK、QQ或真实模型集成测试。完整上游pytest套件未运行。

## 历史基线

其余 `test_*.py` 是公开仓提交 `b0d07b8` 所保留的旧补丁测试，历史统计为7组112项。这些测试未随新接口全部迁移，不代表当前版本通过，也不要用目录通配运行来作为新版验收。

其中 `test_feedback.py` 的旧夹具缺少真实消息身份及结构化通知；`test_memory_dedup.py` 仍要求已经移除的宽松bigram合并。这两组旧预期与安全修复不兼容，应参考新回归入口。其他历史测试本次也没有重新验收。
