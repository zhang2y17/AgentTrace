"""工具层：注册表、契约与四个内置工具。

模块划分（ARCHITECTURE §2）：

- ``base.py``            — ``ToolSpec`` 与参数/返回 Pydantic 模型
- ``registry.py``        — 注册、发现、校验、计时、落库、重试
- ``bootstrap.py``       — 按配置装配实现实例并注册（生产装配只此一处）
- ``document_search.py`` — ``search_documents`` / ``get_document``
- ``analytics.py``       — ``calculate_latency_summary`` / ``calculate_cost_summary``

契约要点（PROJECT_SPEC §3.3）：参数一律经 Pydantic 校验，
校验失败不得调用实现体；统计与成本由确定性代码计算。

``registry`` 与 ``bootstrap`` 的分工：前者管"注册表本身"，
后者管"把实现实例造出来"。分开是为了让测试能注入不同实现
（例如指向临时文档目录的 store），而不必改动注册表。
"""
