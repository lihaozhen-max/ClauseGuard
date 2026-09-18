"""M0 环境与配置验收用例（不依赖 MySQL）。

覆盖：CF-22/CF-23 fail-fast、CF-17/CF-18 硬约束、LM-18 降级、
SPEC §5.3 错误结构、§5.4 错误码表、FR-SYS-03 鉴权、FR-LOG-03 日志脱敏。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.core.errors import ERROR_SPECS, AppError, ErrorCode
from app.core.logging import RedactingFilter
from app.core.security import verify_api_key


def _bare_settings(**overrides) -> Settings:
    """不读 .env 的构造，用于验证 fail-fast 与边界约束。"""
    base = {"db_password": "pwd", "approval_api_key": "ak", "internal_api_key": "ik"}
    base.update(overrides)
    return Settings(_env_file=None, **base)


# ── CF-22 / CF-23：配置来源与 fail-fast ────────────────────────────────────


def test_settings_load_from_env_file(settings: Settings) -> None:
    """CF-22：配置经 pydantic-settings 从 ClauseGuard/.env 读取。"""
    assert settings.app_env == "dev"
    assert settings.db_name == "clauseguard"
    assert settings.db_port == 3306
    assert settings.approval_base_url.startswith("http://127.0.0.1:8100")
    assert settings.db_password, "DB_PASSWORD 必填（CF-04）"


@pytest.mark.parametrize("missing", ["DB_PASSWORD", "APPROVAL_API_KEY", "INTERNAL_API_KEY"])
def test_fail_fast_when_required_missing(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    """CF-23：必填项缺失必须启动失败，且错误信息点名缺失的变量。"""
    monkeypatch.delenv(missing, raising=False)
    kwargs = {"db_password": "pwd", "approval_api_key": "ak", "internal_api_key": "ik"}
    kwargs[missing.lower()] = ""
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, **kwargs)
    assert missing in str(excinfo.value)


# ── CF-17 / CF-18 / LM-18：LLM 硬约束与降级 ───────────────────────────────


def test_llm_max_tokens_floor() -> None:
    """CF-17：max_tokens 必须 ≥ 512，否则推理 token 会吃光配额导致静默失败。"""
    assert _bare_settings(llm_max_tokens=512).llm_max_tokens == 512
    with pytest.raises(ValidationError):
        _bare_settings(llm_max_tokens=64)


def test_llm_temperature_must_be_zero() -> None:
    """CF-18：判定必须可复现，temperature 必须为 0。"""
    assert _bare_settings(llm_temperature=0).llm_temperature == 0.0
    with pytest.raises(ValidationError):
        _bare_settings(llm_temperature=0.7)


def test_llm_degrades_when_key_empty() -> None:
    """LM-18：Key 为空时必须自动降级，而不是启动失败。"""
    assert _bare_settings(llm_enabled=True, llm_api_key="").llm_enabled is False
    assert _bare_settings(llm_enabled=False, llm_api_key="sk-x").llm_enabled is False
    assert _bare_settings(llm_enabled=True, llm_api_key="sk-x").llm_enabled is True


def test_invalid_log_level_rejected() -> None:
    with pytest.raises(ValidationError):
        _bare_settings(app_log_level="VERBOSE")


# ── 派生属性 ───────────────────────────────────────────────────────────────


def test_derived_paths_and_urls(settings: Settings) -> None:
    assert settings.storage_path.is_absolute()
    assert settings.storage_path.name == "contracts"
    assert settings.ocr_model_path.name == "ocr_models"
    assert settings.database_url.startswith("mysql+asyncmy://")
    assert settings.database_url_sync.startswith("mysql+pymysql://")
    assert settings.db_password not in settings.redacted_database_url()
    assert "***" in settings.redacted_database_url()


# ── SPEC §5.3 / §5.4：错误码与统一结构 ─────────────────────────────────────


def test_error_code_table_matches_spec() -> None:
    """§5.4 的 12 个规范错误码必须齐备，且状态影响与表格一致。"""
    spec_codes = {
        "APPROVAL_NOT_FOUND",
        "APPROVAL_API_ERROR",
        "CONTRACT_ATTACHMENT_MISSING",
        "DOWNLOAD_FAILED",
        "EMPTY_CONTRACT_CONTENT",
        "OCR_FAILED",
        "PARSE_FAILED",
        "PARSE_REQUIRED",
        "RULE_EXECUTION_FAILED",
        "LLM_FAILED",
        "COMMENT_WRITE_FAILED",
        "UNAUTHORIZED",
    }
    assert spec_codes <= {c.value for c in ErrorCode}
    assert ERROR_SPECS[ErrorCode.CONTRACT_ATTACHMENT_MISSING].blocked_stage == "parsing"
    assert ERROR_SPECS[ErrorCode.RULE_EXECUTION_FAILED].blocked_stage == "reviewing"
    # LM-14：LLM 失败不得置 blocked
    assert ERROR_SPECS[ErrorCode.LLM_FAILED].blocked_stage is None
    # FR-COM-04 / ST-01-04：回写失败只改 write_status，任务保持 done
    assert ERROR_SPECS[ErrorCode.COMMENT_WRITE_FAILED].write_status_effect == "failed"
    assert "done" in ERROR_SPECS[ErrorCode.COMMENT_WRITE_FAILED].task_status_effect


def test_app_error_body_structure() -> None:
    """§5.3 统一错误结构：error.code / message / task_id / detail。"""
    err = AppError(ErrorCode.APPROVAL_NOT_FOUND, "审批单不存在", task_id=7, detail={"instance_id": "AP-999"})
    body = err.to_body()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "task_id", "detail"}
    assert body["error"]["code"] == "APPROVAL_NOT_FOUND"
    assert err.http_status == 404
    assert AppError(ErrorCode.PARSE_REQUIRED, "未解析").http_status == 409


# ── FR-SYS-03：内部接口鉴权 ────────────────────────────────────────────────


def test_api_key_verification(settings: Settings) -> None:
    verify_api_key(settings.internal_api_key)  # 正确 → 不抛异常
    for bad in (None, "", "wrong-key"):
        with pytest.raises(AppError) as excinfo:
            verify_api_key(bad)
        assert excinfo.value.code is ErrorCode.UNAUTHORIZED
        assert excinfo.value.http_status == 401


# ── FR-LOG-03 / NF-06：日志脱敏 ────────────────────────────────────────────


def test_log_redaction_by_registered_secret_and_pattern(settings: Settings) -> None:
    flt = RedactingFilter(settings.secrets_to_redact)
    line = (
        f"使用密钥 {settings.llm_api_key} 与 {settings.db_password}；"
        f"另见 {settings.redacted_database_url()} password=abc123 token=xyz"
    )
    cleaned = flt.redact(line)
    # 已登记的敏感值必须整体消失
    assert settings.llm_api_key not in cleaned
    assert settings.db_password not in cleaned
    # 未登记但形态明显的密钥走兜底规则
    assert "password=***" in cleaned
    assert "token=***" in cleaned
    assert flt.redact("Authorization: Bearer sk-abcdef1234567890") == "Authorization: Bearer sk-***"


def test_settings_singleton_is_cached() -> None:
    assert get_settings() is get_settings()
