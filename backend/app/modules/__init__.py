"""业务模块（设计 §3.2 / 架构图 FIG-03）。

依赖方向铁律：``api → tools → modules → {clients, llm, db, core}``；
``modules/logging`` 被所有模块依赖，但禁止反向依赖任何业务模块。
"""
