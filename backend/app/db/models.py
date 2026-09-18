"""8 张业务表的 ORM 映射（SPEC §6 DT-01…DT-08）。

映射必须与 ``database/schema.sql`` **逐列一致**（列名、类型、可空性、唯一约束、索引），
由 ``tests/test_m0_env.py::test_orm_matches_live_schema`` 在真实 MySQL 上比对。

命名映射（SPEC §2.1）：
``task_id`` = ``approval_tasks.id`` = ``case_id``；``document_id`` = ``contract_parses.id``；
``review_id`` = ``review_results.id``；``rule_id`` = ``review_rules.id``；
``attachment_id``（对外）= ``approval_attachments.attachment_code``。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.mysql import JSON, LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import MYSQL_TABLE_OPTIONS, Base


class ApprovalTask(Base):
    """DT-01 审批任务（``task_id`` / ``case_id``）。"""

    __tablename__ = "approval_tasks"
    __table_args__ = (
        UniqueConstraint("instance_id", name="uk_approval_tasks_instance_id"),
        Index("ix_approval_tasks_task_status", "task_status"),
        Index("ix_approval_tasks_write_status", "write_status"),
        MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instance_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="唯一业务标识与去重键")
    approval_code: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_title: Mapped[str] = mapped_column(String(255), nullable=False)
    applicant_name: Mapped[str] = mapped_column(String(64), nullable=False)
    apply_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attachment_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    current_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    contract_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    form_data_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    task_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    write_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="not_written")
    blocked_stage: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="parsing / reviewing"
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # created_at 由数据库补默认值；updated_at 每次 UPDATE 刷新（ST-01-05）
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    attachments: Mapped[list["ApprovalAttachment"]] = relationship(
        back_populates="task", lazy="selectin", order_by="ApprovalAttachment.id"
    )
    parses: Mapped[list["ContractParse"]] = relationship(
        back_populates="task", lazy="selectin", order_by="ContractParse.id"
    )
    rule_hits: Mapped[list["RuleHit"]] = relationship(
        back_populates="task", lazy="selectin", order_by="RuleHit.id"
    )
    review: Mapped["ReviewResult | None"] = relationship(back_populates="task", lazy="selectin")
    comment_logs: Mapped[list["CommentLog"]] = relationship(
        back_populates="task", lazy="selectin", order_by="CommentLog.id"
    )


class ApprovalAttachment(Base):
    """DT-02 合同附件。``attachment_code`` 即接口层的 ``attachment_id``。"""

    __tablename__ = "approval_attachments"
    __table_args__ = (
        UniqueConstraint("task_id", "attachment_code", name="uk_approval_attachments_task_attachment"),
        Index("ix_approval_attachments_task_id", "task_id"),
        MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("approval_tasks.id", ondelete="RESTRICT", name="fk_attachments_task"),
        nullable=False,
    )
    attachment_code: Mapped[str] = mapped_column(String(64), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="pdf/docx/png/jpg/jpeg"
    )
    file_path: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="本地路径，禁止对外暴露（FR-SYS-02）"
    )
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    file_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="SHA-256")
    download_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    task: Mapped[ApprovalTask] = relationship(back_populates="attachments")
    parses: Mapped[list["ContractParse"]] = relationship(
        back_populates="attachment", lazy="selectin", order_by="ContractParse.id"
    )


class ContractParse(Base):
    """DT-03 解析结果（``document_id``）。``full_text`` 是证据回验的基准（PS-10）。"""

    __tablename__ = "contract_parses"
    __table_args__ = (
        UniqueConstraint("task_id", "attachment_id", name="uk_contract_parses_task_attachment"),
        Index("ix_contract_parses_task_id", "task_id"),
        MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("approval_tasks.id", ondelete="RESTRICT", name="fk_parses_task"), nullable=False
    )
    attachment_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("approval_attachments.id", ondelete="RESTRICT", name="fk_parses_attachment"),
        nullable=False,
    )
    basic_info_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, comment="8 条 FieldRecord")
    clause_info_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, comment="8 条 FieldRecord")
    full_text: Mapped[str | None] = mapped_column(LONGTEXT, nullable=True)
    page_map_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    parse_mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="text")
    parse_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="success/partial/failed"
    )
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    task: Mapped[ApprovalTask] = relationship(back_populates="parses")
    attachment: Mapped[ApprovalAttachment] = relationship(back_populates="parses")


class ReviewRule(Base):
    """DT-04 审查规则（R001…R011 种子数据，M3 落地）。"""

    __tablename__ = "review_rules"
    __table_args__ = (
        UniqueConstraint("rule_code", name="uk_review_rules_rule_code"),
        MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    rule_code: Mapped[str] = mapped_column(String(16), nullable=False)
    rule_name: Mapped[str] = mapped_column(String(128), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(8), nullable=False, comment="low/medium/high")
    rule_status: Mapped[str] = mapped_column(String(8), nullable=False, server_default="enabled")
    match_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="regex/keyword/threshold/presence/llm_semantic"
    )
    match_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    match_params_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="阈值等参数，禁止硬编码（RL-00-04）"
    )
    suggestion_text: Mapped[str] = mapped_column(Text, nullable=False)
    target_section: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    hits: Mapped[list["RuleHit"]] = relationship(back_populates="rule", lazy="selectin")


class RuleHit(Base):
    """DT-05 规则命中。``risk_level``/``suggestion_text`` 为命中时快照（FR-RULE-09）。"""

    __tablename__ = "rule_hits"
    __table_args__ = (
        UniqueConstraint("task_id", "rule_id", name="uk_rule_hits_task_rule"),
        Index("ix_rule_hits_task_id", "task_id"),
        MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("approval_tasks.id", ondelete="RESTRICT", name="fk_hits_task"), nullable=False
    )
    rule_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("review_rules.id", ondelete="RESTRICT", name="fk_hits_rule"), nullable=False
    )
    risk_level: Mapped[str] = mapped_column(String(8), nullable=False)
    evidence_text: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="必须是合同原文连续子串（PS-10）"
    )
    evidence_position: Mapped[str | None] = mapped_column(String(128), nullable=True)
    suggestion_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    hit_source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="rule")
    hit_status: Mapped[str] = mapped_column(String(16), nullable=False, comment="hit/miss/uncertain")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    task: Mapped[ApprovalTask] = relationship(back_populates="rule_hits")
    rule: Mapped[ReviewRule] = relationship(back_populates="hits")


class ReviewResult(Base):
    """DT-06 审查结果（``review_id``），一任务一结果（UNIQUE task_id）。"""

    __tablename__ = "review_results"
    __table_args__ = (
        UniqueConstraint("task_id", name="uk_review_results_task_id"),
        MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("approval_tasks.id", ondelete="RESTRICT", name="fk_results_task"), nullable=False
    )
    overall_risk_level: Mapped[str] = mapped_column(String(8), nullable=False)
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    focus_points_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    comment_text: Mapped[str] = mapped_column(Text, nullable=False, comment="§4.6.1 模板产物")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    task: Mapped[ApprovalTask] = relationship(back_populates="review")


class CommentLog(Base):
    """DT-07 评论回写日志。``idempotency_key`` 唯一索引是回写幂等的最终保障（FR-COM-02）。"""

    __tablename__ = "comment_logs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uk_comment_logs_idempotency_key"),
        MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("approval_tasks.id", ondelete="RESTRICT", name="fk_comment_logs_task"),
        nullable=False,
    )
    write_status: Mapped[str] = mapped_column(String(16), nullable=False, comment="writing/success/failed")
    write_response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    remark_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    task: Mapped[ApprovalTask] = relationship(back_populates="comment_logs")


class TaskLog(Base):
    """DT-08 任务日志（8 类核心操作，FR-LOG-01）。``log_content`` 必须已脱敏。"""

    __tablename__ = "task_logs"
    __table_args__ = (
        Index("ix_task_logs_task_created", "task_id", "created_at"),
        MYSQL_TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("approval_tasks.id", ondelete="RESTRICT", name="fk_task_logs_task"), nullable=False
    )
    log_level: Mapped[str] = mapped_column(String(8), nullable=False, comment="info/warning/error")
    log_type: Mapped[str] = mapped_column(String(32), nullable=False)
    log_content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


__all__ = [
    "ApprovalTask",
    "ApprovalAttachment",
    "ContractParse",
    "ReviewRule",
    "RuleHit",
    "ReviewResult",
    "CommentLog",
    "TaskLog",
]
