"""M0 数据库集成用例（需要 MySQL 在运行，否则整模块跳过）。

核心目的：保证 ``database/schema.sql``（DDL 权威来源）与 ``app/db/models.py``（ORM 映射）
**不会各改一边**——逐表、逐列、逐唯一索引比对真实 MySQL 的 information_schema。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import mysql
from sqlalchemy.sql.schema import UniqueConstraint

from app.db.base import Base
from app.db import models as _models  # noqa: F401  导入即注册 8 张表
from app.db.session import session_scope

pytestmark = pytest.mark.requires_db

EXPECTED_TABLES = {
    "approval_tasks",
    "approval_attachments",
    "contract_parses",
    "review_rules",
    "rule_hits",
    "review_results",
    "comment_logs",
    "task_logs",
}

#: SQLAlchemy 编译出的类型名 → MySQL information_schema.DATA_TYPE
_TYPE_ALIASES = {"integer": "int"}

_DIALECT = mysql.dialect()


def _declared_type(column: Any) -> str:
    token = str(column.type.compile(dialect=_DIALECT)).split("(")[0].lower()
    return _TYPE_ALIASES.get(token, token)


async def _snapshot() -> dict[str, Any]:
    async with session_scope() as session:
        columns = (
            await session.execute(
                text(
                    """
                    SELECT TABLE_NAME, COLUMN_NAME, IS_NULLABLE, DATA_TYPE,
                           CHARACTER_MAXIMUM_LENGTH, EXTRA, COLUMN_DEFAULT
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                    ORDER BY TABLE_NAME, ORDINAL_POSITION
                    """
                )
            )
        ).all()
        unique_indexes = (
            await session.execute(
                text(
                    """
                    SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME, SEQ_IN_INDEX
                    FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = DATABASE() AND NON_UNIQUE = 0 AND INDEX_NAME <> 'PRIMARY'
                    ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
                    """
                )
            )
        ).all()
        foreign_keys = (
            await session.execute(
                text(
                    """
                    SELECT CONSTRAINT_NAME, TABLE_NAME, DELETE_RULE
                    FROM information_schema.REFERENTIAL_CONSTRAINTS
                    WHERE CONSTRAINT_SCHEMA = DATABASE()
                    ORDER BY CONSTRAINT_NAME
                    """
                )
            )
        ).all()
        tables = (
            await session.execute(
                text(
                    """
                    SELECT TABLE_NAME, TABLE_COLLATION, ENGINE
                    FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA = DATABASE()
                    """
                )
            )
        ).all()
        charset = (await session.execute(text("SELECT @@character_set_database"))).scalar_one()
    return {
        "columns": [dict(r._mapping) for r in columns],
        "unique": [dict(r._mapping) for r in unique_indexes],
        "fks": [dict(r._mapping) for r in foreign_keys],
        "tables": [dict(r._mapping) for r in tables],
        "charset": charset,
    }


@pytest.fixture(scope="module")
def snapshot(live_db: str, db_runner) -> dict[str, Any]:
    return db_runner(_snapshot)


def test_database_charset_is_utf8mb4(snapshot: dict[str, Any]) -> None:
    """SPEC §2.3 / DT-00-02：统一 utf8mb4 + utf8mb4_general_ci。"""
    assert snapshot["charset"] == "utf8mb4"
    for table in snapshot["tables"]:
        assert table["TABLE_COLLATION"] == "utf8mb4_general_ci", table
        assert table["ENGINE"] == "InnoDB", table


def test_all_eight_tables_exist(snapshot: dict[str, Any]) -> None:
    """DT-01…DT-08 共 8 张业务表。"""
    actual = {t["TABLE_NAME"] for t in snapshot["tables"]}
    assert EXPECTED_TABLES <= actual, f"缺表：{EXPECTED_TABLES - actual}"
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_orm_matches_live_schema(snapshot: dict[str, Any]) -> None:
    """ORM 与 schema.sql 逐列一致（列名 / 类型 / 可空性 / varchar 长度）。"""
    live: dict[str, dict[str, dict[str, Any]]] = {}
    for row in snapshot["columns"]:
        live.setdefault(row["TABLE_NAME"], {})[row["COLUMN_NAME"]] = row

    problems: list[str] = []
    for table_name in sorted(EXPECTED_TABLES):
        table = Base.metadata.tables[table_name]
        live_cols = live.get(table_name, {})
        declared_names = {c.name for c in table.columns}
        if declared_names != set(live_cols):
            problems.append(
                f"{table_name} 列集合不一致：ORM 独有={declared_names - set(live_cols)}，"
                f"DB 独有={set(live_cols) - declared_names}"
            )
            continue
        for column in table.columns:
            actual = live_cols[column.name]
            if _declared_type(column) != actual["DATA_TYPE"]:
                problems.append(
                    f"{table_name}.{column.name} 类型不符：ORM={_declared_type(column)} DB={actual['DATA_TYPE']}"
                )
            expected_nullable = "YES" if column.nullable else "NO"
            if expected_nullable != actual["IS_NULLABLE"]:
                problems.append(
                    f"{table_name}.{column.name} 可空性不符：ORM={expected_nullable} DB={actual['IS_NULLABLE']}"
                )
            if actual["DATA_TYPE"] == "varchar" and column.type.length != actual["CHARACTER_MAXIMUM_LENGTH"]:
                problems.append(
                    f"{table_name}.{column.name} 长度不符：ORM={column.type.length} "
                    f"DB={actual['CHARACTER_MAXIMUM_LENGTH']}"
                )
    assert not problems, "ORM 与 schema.sql 不一致：\n" + "\n".join(problems)


def test_primary_keys_are_auto_increment(snapshot: dict[str, Any]) -> None:
    """DT-00-01：所有表主键 id 为 BIGINT AUTO_INCREMENT。"""
    ids = {row["TABLE_NAME"]: row for row in snapshot["columns"] if row["COLUMN_NAME"] == "id"}
    assert set(ids) == EXPECTED_TABLES
    for name, row in ids.items():
        assert row["DATA_TYPE"] == "bigint", name
        assert row["IS_NULLABLE"] == "NO", name
        assert "auto_increment" in row["EXTRA"], name


def test_orm_matches_live_unique_indexes(snapshot: dict[str, Any]) -> None:
    """⭐ 唯一索引是幂等性的最终保障（DT-00-05 / 架构图 FIG-11）。"""
    live: dict[str, dict[str, list[str]]] = {name: {} for name in EXPECTED_TABLES}
    for row in snapshot["unique"]:
        live[row["TABLE_NAME"]].setdefault(row["INDEX_NAME"], []).append(row["COLUMN_NAME"])

    declared: dict[str, dict[str, list[str]]] = {}
    for table_name, table in Base.metadata.tables.items():
        declared[table_name] = {
            c.name: [col.name for col in c.columns]
            for c in table.constraints
            if isinstance(c, UniqueConstraint)
        }

    assert live == declared, f"唯一索引不一致：DB={live} ORM={declared}"

    # 4 道防线必须齐备（SPEC §6 标注 ⭐ 的约束）
    assert live["approval_tasks"]["uk_approval_tasks_instance_id"] == ["instance_id"]
    assert live["approval_attachments"]["uk_approval_attachments_task_attachment"] == [
        "task_id",
        "attachment_code",
    ]
    assert live["contract_parses"]["uk_contract_parses_task_attachment"] == ["task_id", "attachment_id"]
    assert live["rule_hits"]["uk_rule_hits_task_rule"] == ["task_id", "rule_id"]
    assert live["review_results"]["uk_review_results_task_id"] == ["task_id"]
    assert live["comment_logs"]["uk_comment_logs_idempotency_key"] == ["idempotency_key"]
    assert live["review_rules"]["uk_review_rules_rule_code"] == ["rule_code"]


def test_foreign_keys_are_on_delete_restrict(snapshot: dict[str, Any]) -> None:
    """DT-00-04：外键一律 ON DELETE RESTRICT，审查结果与日志禁止被级联删除。"""
    assert len(snapshot["fks"]) == 8, snapshot["fks"]
    for row in snapshot["fks"]:
        assert row["DELETE_RULE"] == "RESTRICT", row


def test_key_column_defaults(snapshot: dict[str, Any]) -> None:
    """§2.2 枚举列的默认值必须与 SPEC 一致。"""
    wanted = {
        ("approval_tasks", "task_status"): "pending",
        ("approval_tasks", "write_status"): "not_written",
        ("approval_tasks", "retry_count"): "0",
        ("approval_attachments", "download_status"): "pending",
        ("contract_parses", "parse_mode"): "text",
        ("review_rules", "rule_status"): "enabled",
        ("rule_hits", "hit_source"): "rule",
    }
    actual = {
        (row["TABLE_NAME"], row["COLUMN_NAME"]): row["COLUMN_DEFAULT"] for row in snapshot["columns"]
    }
    for key, expected in wanted.items():
        assert actual[key] == expected, f"{key} 默认值应为 {expected}，实际 {actual[key]}"
