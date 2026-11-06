# 演示开发集

`python manage.py seed_demo --password ...` 为两个租户分别创建 20 条问题、四份自编 Markdown 语料和独立文档 UUID。

数据源在 `backend/apps/users/management/commands/seed_demo.py`，每条 gold evidence 保存文档/版本 UUID、文件 SHA-256 和原文。评估运行校验冻结版本仍为当前 active 版本，变化时该题保存错误，不把新语料冒充原始实验。

该集合是开发调试集，不是保留测试集；不覆盖安全攻击题、冲突问题、复杂 PDF 或人工事实支撑标注。seed 不覆盖已有版本或用户密码。
