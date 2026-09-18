"""ORM 声明式基类（SPEC DT-00-01…DT-00-05 的落点）。

DDL 的**权威来源**是 ``database/schema.sql``（设计 §13）；本包内的模型是它的 ORM 映射，
两者由 ``tests/test_m0_env.py`` 的一致性用例逐表逐列比对，禁止只改一边。
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase

#: 所有表统一字符集/排序规则/引擎（DT-00-02）
MYSQL_TABLE_OPTIONS: dict[str, str] = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_general_ci",
}


class Base(DeclarativeBase):
    """项目内全部 ORM 模型的基类。"""

    pass
