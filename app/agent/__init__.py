"""Agent 层：LangGraph 状态图、节点、LLM 网关与 Trace 中间件。

模块划分（ARCHITECTURE §2）：

- ``state.py``      — LangGraph 状态 ``TypedDict``
- ``llm.py``        — ``LLMGateway`` 抽象与三个 provider 实现
- ``nodes/``        — 5 个节点，一个文件一个节点
- ``graph.py``      — ``StateGraph`` 装配与条件边
- ``middleware.py`` — ``TraceRecorder``，把节点执行包成 Trace 事件
"""
