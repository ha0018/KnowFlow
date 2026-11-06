# 召回实验记录

`demo-dense.json` 与 `demo-hybrid.json` 是在本地 SQLite、四份自编 Markdown、20 条开发题以及 `demo-hash-v1` 下实际运行的逐题输出。

这两项实验只验证开发集的文档召回流程，不可用于宣称真实模型召回或生产 RAG 效果。小型同词开发题在两种配置下都能命中，没有证据表明 Hybrid 在该样本上优于 Dense。

复现：

```powershell
cd backend
uv run python manage.py evaluate_demo --mode dense --output ../evals/reports/demo-dense.json
uv run python manage.py evaluate_demo --mode hybrid --output ../evals/reports/demo-hybrid.json
```

报告保留 run/case/chunk ID、问题、每题指标/错误、环境、依赖锁哈希与语料版本信息。全新初始化时 UUID 会改变；内容及指标定义保持一致。真实模型实验应另外保存模型版本、独立保留集、源码修订和硬件/并发信息。
