"""LLM 抽象与实现（SPEC §10）。

- ``LLMClient`` 抽象接口
- ``DeepSeekClient`` 实现（CF-13…CF-18）
- ``NullLLMClient`` 降级实现（LM-15/LM-16/LM-18）

M3 实现；业务模块只依赖抽象，便于切换 OpenAI 兼容端点。
"""
