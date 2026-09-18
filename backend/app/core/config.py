"""配置层（SPEC §11 CF-01…CF-24）。

必须遵守的三条硬约束：

- **CF-22**：所有配置经 ``pydantic-settings`` 从环境变量 / ``.env`` 读取，
  代码中**禁止**散落 ``os.getenv``。
- **CF-23**：启动时校验必填项，缺失则启动失败并给出明确错误（fail-fast）。
- **CF-24**：``.env`` 由 ``.gitignore`` 忽略，仓库中仅保留 ``.env.example``。

配置文件位置固定为项目根目录 ``ClauseGuard/.env``（与 ``.env.example`` 同级），
不随进程工作目录变化——避免"在 backend/ 下启动就读不到配置"的坑。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: 项目根目录（``ClauseGuard/``）——本文件位于 ``backend/app/core/config.py``
PROJECT_ROOT: Path = Path(__file__).resolve().parents[3]
ENV_FILE: Path = PROJECT_ROOT / ".env"

_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}


def _is_ascii_path(path: Path) -> bool:
    try:
        str(path).encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def default_ocr_model_dir() -> Path:
    """ASCII 安全的 OCR 模型缓存目录（机器本地，不提交仓库）。

    Windows：``%LOCALAPPDATA%\\ClauseGuard\\ocr_models``；其他平台：``~/.cache/clauseguard/ocr_models``。
    """
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return (Path(local_appdata) / "ClauseGuard" / "ocr_models").resolve()
    return (Path.home() / ".cache" / "clauseguard" / "ocr_models").resolve()


class Settings(BaseSettings):
    """全部运行时配置。字段名小写，对应环境变量大写（CF-01…CF-21）。"""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── CF-01…CF-03 应用 ────────────────────────────────────────────
    app_env: str = "dev"
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    app_log_level: str = "INFO"

    # ── CF-04 MySQL ────────────────────────────────────────────────
    db_host: str = "127.0.0.1"
    db_port: int = Field(default=3306, ge=1, le=65535)
    db_user: str = "clauseguard"
    db_password: str = ""  # 必填，见 _require
    db_name: str = "clauseguard"

    # ── CF-05…CF-07 审批系统与内部鉴权 ──────────────────────────────
    approval_base_url: str = "http://127.0.0.1:8100"
    approval_api_key: str = ""  # 必填
    internal_api_key: str = ""  # 必填

    # ── CF-08 附件存储（不对外公开，FR-SYS-02）──────────────────────
    storage_dir: str = "./storage/contracts"

    # ── CF-09…CF-11 规则阈值与扫描页判定 ────────────────────────────
    rule_prepay_max_ratio: float = Field(default=0.30, gt=0, le=1)
    rule_payment_max_days: int = Field(default=90, ge=0)
    ocr_scan_page_min_chars: int = Field(default=20, ge=0)

    # ── CF-12…CF-18 LLM ───────────────────────────────────────────
    llm_enabled: bool = True
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-flash"
    llm_timeout_seconds: int = Field(default=60, ge=1)
    llm_max_tokens: int = Field(default=1024, ge=512)  # CF-17：推理 token 计入配额
    llm_temperature: float = 0.0

    # ── CF-19…CF-21 OCR ───────────────────────────────────────────
    ocr_lang: str = "ch"
    ocr_use_gpu: bool = False
    #: CF-21 的模型缓存目录。**留空表示自动选择 ASCII 安全目录**——
    #: Paddle Inference 在 Windows 上无法读取含非 ASCII 字符路径下的模型文件
    #: （实测：中文路径下 create_predictor 报 json parse error，纯 ASCII 路径正常，
    #: 详见 docs/M2-验收记录.md 的 R-01）。
    ocr_model_dir: str = ""
    #: 补充配置（SPEC §11 未列）：paddle 3.3.1 的 PIR 执行器在 oneDNN/MKLDNN 路径上
    #: 存在转换缺陷（ConvertPirAttribute2RuntimeAttribute not support），默认关闭。
    ocr_enable_mkldnn: bool = False

    # ── 校验器 ────────────────────────────────────────────────────

    @field_validator("app_log_level")
    @classmethod
    def _check_log_level(cls, v: str) -> str:
        level = v.strip().upper()
        if level not in _LOG_LEVELS:
            raise ValueError(f"APP_LOG_LEVEL 必须是 {sorted(_LOG_LEVELS)} 之一，当前为 {v!r}")
        return level

    @field_validator("llm_temperature")
    @classmethod
    def _check_temperature(cls, v: float) -> float:
        # CF-18：判定必须可复现，temperature 必须为 0
        if v != 0:
            raise ValueError(f"LLM_TEMPERATURE 必须为 0（CF-18，判定需可复现），当前为 {v!r}")
        return v

    @model_validator(mode="after")
    def _require(self) -> Settings:
        """CF-23 fail-fast：必填项缺失即启动失败，并一次列出全部缺失项。"""
        required = {
            "DB_PASSWORD": self.db_password,
            "APPROVAL_API_KEY": self.approval_api_key,
            "INTERNAL_API_KEY": self.internal_api_key,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ValueError(
                "配置缺失，启动中止（CF-23）："
                + "、".join(missing)
                + f"。请在 {ENV_FILE} 中补齐（模板见 .env.example）。"
            )
        return self

    @model_validator(mode="after")
    def _degrade_llm_without_key(self) -> Settings:
        """LM-18：Key 为空时必须仍能完成 AC01–AC19 全闭环 → 自动降级，不启动失败。

        SPEC CF-14 把 ``LLM_API_KEY`` 标为必填，但 LM-18 明确要求"Key 为空时系统仍能
        跑通全闭环"。二者冲突时按 §0.2 取更严者不成立（会导致无 Key 无法启动），
        故此处按 LM-18 处理：置 ``llm_enabled=False``，由调用方记录降级日志。
        """
        if self.llm_enabled and not self.llm_api_key.strip():
            self.llm_enabled = False
        return self

    # ── 派生属性 ──────────────────────────────────────────────────

    @property
    def storage_path(self) -> Path:
        """附件存储根目录（CF-08）。相对路径按项目根目录解析，不随 CWD 漂移。"""
        return self._resolve(self.storage_dir)

    @property
    def ocr_model_path(self) -> Path:
        """OCR 模型缓存目录（CF-21）。

        未显式配置时返回 :func:`default_ocr_model_dir`（ASCII 安全目录，见字段注释）。
        """
        if not self.ocr_model_dir.strip():
            return default_ocr_model_dir()
        return self._resolve(self.ocr_model_dir)

    @property
    def ocr_cache_dir(self) -> Path:
        """真正交给 Paddle 的目录：**保证 ASCII**，否则回退到 ASCII 默认目录。

        Paddle Inference 打不开非 ASCII 路径，硬用它只会得到
        ``json parse error: attempting to parse an empty input`` 这类误导性错误，
        因此这里主动规避，并由 OCR 引擎记录告警。
        """
        configured = self.ocr_model_path
        if _is_ascii_path(configured):
            return configured
        return default_ocr_model_dir()

    @staticmethod
    def _resolve(raw: str) -> Path:
        path = Path(raw)
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()

    def _db_url(self, driver: str) -> str:
        return (
            f"mysql+{driver}://{quote_plus(self.db_user)}:{quote_plus(self.db_password)}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )

    @property
    def database_url(self) -> str:
        """SQLAlchemy 异步连接串（OPEN-01：主选 asyncmy）。"""
        return self._db_url("asyncmy")

    @property
    def database_url_sync(self) -> str:
        """同步连接串（OPEN-01 兜底：pymysql），供脚本/迁移工具使用。"""
        return self._db_url("pymysql")

    def redacted_database_url(self) -> str:
        """可安全写入日志的连接串（FR-LOG-03 禁止记录数据库连接串明文）。"""
        return f"mysql://{self.db_user}:***@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def secrets_to_redact(self) -> tuple[str, ...]:
        """需要从日志中抹除的敏感值（FR-LOG-03 / NF-06）。"""
        return tuple(
            s for s in (self.db_password, self.approval_api_key, self.internal_api_key, self.llm_api_key) if s
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级单例。测试中可通过 ``get_settings.cache_clear()`` 重载。"""
    return Settings()
