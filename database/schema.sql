-- ============================================================================
-- ClauseGuard 合同审批审查系统 — 建表脚本
-- 规范依据：docs/SPEC.md §6（DT-00-01…DT-00-05、DT-01…DT-08）
-- 本文件是 DDL 的**权威来源**；backend/app/db/models.py 是它的 ORM 映射，
-- 两者由 tests/test_m0_env.py::test_orm_matches_live_schema 逐列比对，禁止只改一边。
--
-- 通用要求：
--   DT-00-01 所有表有主键 id（BIGINT AUTO_INCREMENT）
--   DT-00-02 所有表字符集 utf8mb4 / 排序规则 utf8mb4_general_ci
--   DT-00-03 时间列 DATETIME，存 UTC（容器以 --default-time-zone=+00:00 启动）
--   DT-00-04 外键 ON DELETE RESTRICT（结果与日志禁止被级联删除）
--   DT-00-05 ⭐ 唯一索引是幂等性的最终保障
--
-- 用法：由 docker compose 在数据卷首次初始化时自动执行；
--       手工重跑也安全（CREATE ... IF NOT EXISTS）。
-- ============================================================================

SET NAMES utf8mb4;

CREATE DATABASE IF NOT EXISTS `clauseguard`
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_general_ci;

USE `clauseguard`;

-- ── DT-01 approval_tasks 审批任务（id 即 task_id / case_id）──────────────────
CREATE TABLE IF NOT EXISTS `approval_tasks` (
    `id`               BIGINT       NOT NULL AUTO_INCREMENT COMMENT 'PK，即 task_id / case_id',
    `instance_id`      VARCHAR(64)  NOT NULL COMMENT '⭐唯一业务标识与去重键',
    `approval_code`    VARCHAR(64)  NOT NULL COMMENT '审批编号',
    `approval_title`   VARCHAR(255) NOT NULL,
    `applicant_name`   VARCHAR(64)  NOT NULL,
    `apply_time`       DATETIME     NULL,
    `attachment_count` INT          NOT NULL DEFAULT 0,
    `current_status`   VARCHAR(32)  NULL COMMENT '审批系统侧状态',
    `contract_type`    VARCHAR(64)  NULL,
    `form_data_json`   JSON         NULL COMMENT '审批表单字段',
    `task_status`      VARCHAR(16)  NOT NULL DEFAULT 'pending'     COMMENT 'pending/parsing/reviewing/blocked/done',
    `write_status`     VARCHAR(16)  NOT NULL DEFAULT 'not_written' COMMENT 'not_written/writing/success/failed',
    `blocked_stage`    VARCHAR(16)  NULL COMMENT 'parsing / reviewing',
    `error_code`       VARCHAR(64)  NULL COMMENT 'SPEC §5.4 错误码',
    `error_message`    TEXT         NULL,
    `retry_count`      INT          NOT NULL DEFAULT 0,
    `created_at`       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_approval_tasks_instance_id` (`instance_id`),
    KEY `ix_approval_tasks_task_status` (`task_status`),
    KEY `ix_approval_tasks_write_status` (`write_status`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_general_ci COMMENT = 'DT-01 审批任务';

-- ── DT-02 approval_attachments 合同附件 ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS `approval_attachments` (
    `id`              BIGINT       NOT NULL AUTO_INCREMENT,
    `task_id`         BIGINT       NOT NULL,
    `attachment_code` VARCHAR(64)  NOT NULL COMMENT '审批系统侧附件编号，即 attachment_id',
    `file_name`       VARCHAR(255) NOT NULL,
    `file_type`       VARCHAR(16)  NOT NULL COMMENT 'pdf/docx/png/jpg/jpeg',
    `file_path`       VARCHAR(512) NULL COMMENT '本地路径，禁止对外暴露（FR-SYS-02）',
    `file_size`       BIGINT       NULL COMMENT '字节',
    `file_checksum`   VARCHAR(64)  NULL COMMENT 'SHA-256 十六进制',
    `download_status` VARCHAR(16)  NOT NULL DEFAULT 'pending' COMMENT 'pending/success/failed',
    `created_at`      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_approval_attachments_task_attachment` (`task_id`, `attachment_code`),
    KEY `ix_approval_attachments_task_id` (`task_id`),
    CONSTRAINT `fk_attachments_task` FOREIGN KEY (`task_id`)
        REFERENCES `approval_tasks` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_general_ci COMMENT = 'DT-02 合同附件';

-- ── DT-03 contract_parses 解析结果（id 即 document_id）──────────────────────
CREATE TABLE IF NOT EXISTS `contract_parses` (
    `id`               BIGINT   NOT NULL AUTO_INCREMENT,
    `task_id`          BIGINT   NOT NULL,
    `attachment_id`    BIGINT   NOT NULL COMMENT 'FK → approval_attachments.id',
    `basic_info_json`  JSON     NOT NULL COMMENT 'FieldRecord 数组，8 条',
    `clause_info_json` JSON     NOT NULL COMMENT 'FieldRecord 数组，8 条',
    `full_text`        LONGTEXT NULL COMMENT '清洗后全文，证据校验基准（PS-10）',
    `page_map_json`    JSON     NULL COMMENT '页码 → 文本偏移',
    `parse_mode`       VARCHAR(16) NOT NULL DEFAULT 'text' COMMENT 'text / ocr，决定 position 格式',
    `parse_status`     VARCHAR(16) NOT NULL COMMENT 'success/partial/failed',
    `parse_error`      TEXT     NULL COMMENT '失败原因（FR-PARSE-07 禁止只返回空结果）',
    `created_at`       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_contract_parses_task_attachment` (`task_id`, `attachment_id`),
    KEY `ix_contract_parses_task_id` (`task_id`),
    CONSTRAINT `fk_parses_task` FOREIGN KEY (`task_id`)
        REFERENCES `approval_tasks` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT `fk_parses_attachment` FOREIGN KEY (`attachment_id`)
        REFERENCES `approval_attachments` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_general_ci COMMENT = 'DT-03 合同解析结果';

-- ── DT-04 review_rules 审查规则（R001…R011）─────────────────────────────────
CREATE TABLE IF NOT EXISTS `review_rules` (
    `id`                BIGINT       NOT NULL AUTO_INCREMENT COMMENT '即 rule_id',
    `rule_code`         VARCHAR(16)  NOT NULL COMMENT '⭐R001…R011',
    `rule_name`         VARCHAR(128) NOT NULL,
    `risk_level`        VARCHAR(8)   NOT NULL COMMENT 'low/medium/high',
    `rule_status`       VARCHAR(8)   NOT NULL DEFAULT 'enabled' COMMENT 'enabled/disabled',
    `match_mode`        VARCHAR(16)  NOT NULL COMMENT 'regex/keyword/threshold/presence/llm_semantic',
    `match_text`        TEXT         NULL COMMENT '正则/关键词/表达式载体',
    `match_params_json` JSON         NULL COMMENT '阈值等参数，禁止硬编码（RL-00-04）',
    `suggestion_text`   TEXT         NOT NULL COMMENT '处理建议',
    `target_section`    VARCHAR(32)  NULL COMMENT '限定匹配部位，如 payment',
    `updated_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_review_rules_rule_code` (`rule_code`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_general_ci COMMENT = 'DT-04 审查规则';

-- ── DT-05 rule_hits 规则命中 ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS `rule_hits` (
    `id`                BIGINT      NOT NULL AUTO_INCREMENT,
    `task_id`           BIGINT      NOT NULL,
    `rule_id`           BIGINT      NOT NULL,
    `risk_level`        VARCHAR(8)  NOT NULL COMMENT '命中时快照（FR-RULE-09）',
    `evidence_text`     TEXT        NULL COMMENT '必须是合同原文连续子串（PS-10）',
    `evidence_position` VARCHAR(128) NULL COMMENT '§2.6 格式',
    `suggestion_text`   TEXT        NULL COMMENT '命中时快照',
    `hit_source`        VARCHAR(16) NOT NULL DEFAULT 'rule' COMMENT 'rule / llm',
    `hit_status`        VARCHAR(16) NOT NULL COMMENT 'hit/miss/uncertain',
    `created_at`        DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_rule_hits_task_rule` (`task_id`, `rule_id`),
    KEY `ix_rule_hits_task_id` (`task_id`),
    CONSTRAINT `fk_hits_task` FOREIGN KEY (`task_id`)
        REFERENCES `approval_tasks` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT `fk_hits_rule` FOREIGN KEY (`rule_id`)
        REFERENCES `review_rules` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_general_ci COMMENT = 'DT-05 规则命中';

-- ── DT-06 review_results 审查结果（id 即 review_id）─────────────────────────
CREATE TABLE IF NOT EXISTS `review_results` (
    `id`                 BIGINT      NOT NULL AUTO_INCREMENT,
    `task_id`            BIGINT      NOT NULL COMMENT '⭐一任务一结果',
    `overall_risk_level` VARCHAR(8)  NOT NULL COMMENT 'low/medium/high',
    `summary_text`       TEXT        NOT NULL COMMENT '中文摘要',
    `focus_points_json`  JSON        NOT NULL COMMENT 'string 数组',
    `comment_text`       TEXT        NOT NULL COMMENT '§4.6.1 模板产物',
    `created_at`         DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_review_results_task_id` (`task_id`),
    CONSTRAINT `fk_results_task` FOREIGN KEY (`task_id`)
        REFERENCES `approval_tasks` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_general_ci COMMENT = 'DT-06 审查结果';

-- ── DT-07 comment_logs 评论回写日志 ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS `comment_logs` (
    `id`                  BIGINT      NOT NULL AUTO_INCREMENT,
    `task_id`             BIGINT      NOT NULL,
    `write_status`        VARCHAR(16) NOT NULL COMMENT 'writing/success/failed',
    `write_response_text` TEXT        NULL COMMENT '审批系统返回原文 / 错误信息',
    `remark_id`           VARCHAR(64) NULL COMMENT '审批系统评论 ID',
    `idempotency_key`     VARCHAR(64) NOT NULL COMMENT '⭐instance_id + review_id 的 SHA-256',
    `created_at`          DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_comment_logs_idempotency_key` (`idempotency_key`),
    CONSTRAINT `fk_comment_logs_task` FOREIGN KEY (`task_id`)
        REFERENCES `approval_tasks` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_general_ci COMMENT = 'DT-07 评论回写日志';

-- ── DT-08 task_logs 任务日志 ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS `task_logs` (
    `id`          BIGINT      NOT NULL AUTO_INCREMENT,
    `task_id`     BIGINT      NOT NULL,
    `log_level`   VARCHAR(8)  NOT NULL COMMENT 'info/warning/error',
    `log_type`    VARCHAR(32) NOT NULL COMMENT 'pull/download/parse/ocr/extract/rule/save/write_comment/retry',
    `log_content` TEXT        NOT NULL COMMENT '已脱敏（FR-LOG-03）',
    `created_at`  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `ix_task_logs_task_created` (`task_id`, `created_at`),
    CONSTRAINT `fk_task_logs_task` FOREIGN KEY (`task_id`)
        REFERENCES `approval_tasks` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_general_ci COMMENT = 'DT-08 任务日志';
