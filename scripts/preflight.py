"""环境预检（Preflight）：在一台新机器上开始之前，先把"会不会踩坑"查清楚。

回答一个问题：**"我这台机器现在能跑起来吗？差什么？怎么补？"**

用法::

    uv run --project backend python scripts/preflight.py
    uv run --project backend python scripts/preflight.py --json     # 机器可读

退出码：0 = 必需项全部通过；1 = 有必需项不通过（可直接用于 CI / 交付自检）。

设计取舍：

- **只读**：不改任何配置、不建表、不写库；
- **不打印密钥**：只报告"是否已配置"与长度，绝不回显值（NF-06）；
- 不引入新依赖：读 ``.env`` 用十行解析器，端口用 ``socket``，数据库用项目已有的 ``pymysql``；
- 必需项（❌）与提醒项（⚠️）分开：后者不影响启动（例如没装 Chrome 只是跑不了浏览器用例）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
SAMPLES = PROJECT_ROOT / "sample_contracts"
MODELS_DIR = Path.home() / "AppData" / "Local" / "ClauseGuard" / "ocr_models"

# 输出被重定向/捕获时用 UTF-8（便于 CI 与日志工具解析）；人在 GBK 控制台直接跑时保持原样，
# 否则好好的中文反而会变成乱码。
if not sys.stdout.isatty():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OK = "[OK]"
WARN = "[!]"
BAD = "[X]"

#: 启动所必需的键（CF-01…CF-24 的必填子集）
REQUIRED_KEYS = (
    "DB_PASSWORD",
    "DB_USER",
    "DB_NAME",
    "APPROVAL_BASE_URL",
    "APPROVAL_API_KEY",
    "INTERNAL_API_KEY",
)
#: 可为空但值得提醒的键
OPTIONAL_KEYS = ("DB_ROOT_PASSWORD", "LLM_API_KEY")
#: DT-01…DT-08
EXPECTED_TABLES = (
    "approval_tasks",
    "approval_attachments",
    "contract_parses",
    "review_rules",
    "rule_hits",
    "review_results",
    "comment_logs",
    "task_logs",
)
#: 样例附件（AP-004 的附件**故意不存在**，用于 AC16）
EXPECTED_SAMPLES = (
    "AP-001_采购合同.pdf",
    "AP-002_服务合同.pdf",
    "AP-003_扫描件.png",
    "AP-005_空文件.pdf",
    "expected_results.json",
)
MISSING_BY_DESIGN = "AP-004_办公用品采购合同.pdf"


@dataclass
class Check:
    name: str
    level: str  # ok / warn / bad
    detail: str
    fix: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, *, fix: str = "", required: bool = True) -> bool:
        level = "ok" if ok else ("bad" if required else "warn")
        self.checks.append(Check(name, level, detail, fix if not ok else ""))
        return ok

    @property
    def failed(self) -> list[Check]:
        return [item for item in self.checks if item.level == "bad"]


# ── 小工具 ─────────────────────────────────────────────────────────────────
def run(command: list[str], timeout: float = 20.0) -> tuple[bool, str]:
    """执行命令并返回 (是否成功, 首行输出)。失败不抛异常——预检本身不能崩。

    Windows 上 ``npm`` / ``uv`` 往往是 ``.cmd`` 包装脚本，``subprocess`` 在
    ``shell=False`` 时**不会**按 PATHEXT 解析，必须先用 ``shutil.which`` 找到真实路径，
    再决定是否需要经 ``cmd.exe /c`` 运行。
    """
    executable = shutil.which(command[0])
    if executable is None:
        return False, f"未找到命令 {command[0]}"
    try:
        if executable.lower().endswith((".cmd", ".bat")):
            # ``.cmd``/``.bat`` 不能被 CreateProcess 直接执行；交给 cmd.exe，并**自己**给路径加引号
            # （npm 常位于 ``D:\Program_Files_(x86)\...``，未加引号时括号会被 cmd 当语法字符截断）。
            command_line = " ".join([f'"{executable}"', *command[1:]])
            completed = subprocess.run(  # noqa: S602 - 参数由本文件的常量与 which() 结果构成
                command_line, capture_output=True, text=True, timeout=timeout, shell=True
            )
        else:
            completed = subprocess.run(  # noqa: S603
                [executable, *command[1:]], capture_output=True, text=True, timeout=timeout
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    text = (completed.stdout or completed.stderr or "").strip().splitlines()
    return completed.returncode == 0, text[0] if text else ""


def parse_env(path: Path) -> dict[str, str]:
    """极简 .env 解析：``KEY=VALUE``，忽略注释与空行，去掉成对引号。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def port_state(port: int) -> str:
    """返回 free / listening（只看能否连上，不区分 IPv4/IPv6）。"""
    with socket.socket() as sock:
        sock.settimeout(0.6)
        try:
            sock.connect(("127.0.0.1", port))
            return "listening"
        except OSError:
            return "free"


def mask(value: str) -> str:
    """只报告长度，绝不回显密钥（NF-06）。"""
    return f"已配置（{len(value)} 字符）" if value else "空"


# ── 检查项 ─────────────────────────────────────────────────────────────────
def check_host(report: Report) -> None:
    major, minor = sys.version_info[:2]
    report.add(
        "Python 版本",
        (major, minor) == (3, 13),
        f"{major}.{minor}.{sys.version_info[2]}",
        fix="paddlepaddle 无 3.14 轮子，必须 3.13：uv python install 3.13（设计 R1）",
    )

    found, version = run(["uv", "--version"])
    report.add("uv", found, version or "未找到", fix="安装 uv：https://docs.astral.sh/uv/")

    found, version = run(["node", "--version"])
    node_ok = False
    if found and version.startswith("v"):
        try:
            node_ok = int(version[1:].split(".")[0]) >= 20
        except ValueError:
            node_ok = False
    report.add("Node.js ≥ 20", node_ok, version or "未找到", fix="安装 Node 20+：https://nodejs.org/")

    npm_ok, npm_version = run(["npm", "--version"])
    report.add("npm", npm_ok, npm_version, fix="随 Node 一起安装")

    docker_ok, docker_version = run(["docker", "--version"])
    report.add("Docker", docker_ok, docker_version or "未找到", fix="安装 Docker Desktop")

    if docker_ok:
        listed = subprocess.run(  # noqa: S603
            ["docker", "ps", "--filter", "name=clauseguard-mysql", "--format", "{{.Status}}"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        report.add(
            "MySQL 容器",
            bool(listed),
            listed or "未运行",
            fix="cd database; docker compose --env-file ../.env up -d",
        )
    else:
        report.add("MySQL 容器", False, "Docker 不可用，跳过", fix="先装 Docker Desktop")

    # 浏览器与 OCR 模型只影响可选能力，算提醒项
    chrome = next(
        (
            candidate
            for candidate in (
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            )
            if Path(candidate).is_file()
        ),
        None,
    )
    report.add(
        "Chrome / Edge（浏览器端到端用例）",
        bool(chrome),
        Path(chrome).name if chrome else "未找到",
        fix="安装 Chrome 或 Edge；没有只会跳过 -m ui 的用例",
        required=False,
    )

    if MODELS_DIR.is_dir():
        size_mb = sum(p.stat().st_size for p in MODELS_DIR.rglob("*") if p.is_file()) / 1024 / 1024
        report.add("PaddleOCR 模型缓存", size_mb > 100, f"{MODELS_DIR}（{size_mb:.0f} MB）")
    else:
        report.add(
            "PaddleOCR 模型缓存",
            False,
            "不存在",
            fix="首次 OCR 会自动联网下载（数百 MB）；演示前务必预热，见 docs/演示说明.md §0",
            required=False,
        )


def check_config(report: Report) -> dict[str, str]:
    env = parse_env(ENV_FILE)
    report.add(
        ".env 存在",
        ENV_FILE.is_file(),
        str(ENV_FILE),
        fix="Copy-Item .env.example .env 后填写（CF-24：.env 不入库）",
    )
    if not ENV_FILE.is_file():
        return {}

    missing = [key for key in REQUIRED_KEYS if not env.get(key)]
    report.add(
        "必填配置项齐全",
        not missing,
        "全部已配置" if not missing else f"缺少：{'、'.join(missing)}",
        fix="编辑 .env 补齐；缺项会让服务启动即失败（CF-23）",
    )
    for key in OPTIONAL_KEYS:
        ok = bool(env.get(key))
        fix = (
            "留空则自动降级为纯规则 + 模板摘要，闭环照样跑通（LM-18）"
            if key == "LLM_API_KEY"
            else "仅 docker compose 首次初始化需要"
        )
        report.add(f"{key}（可选）", ok, mask(env.get(key, "")), fix=fix, required=False)
    return env


def check_assets(report: Report) -> None:
    missing = [name for name in EXPECTED_SAMPLES if not (SAMPLES / name).is_file()]
    report.add(
        "样例合同与期望结果",
        not missing,
        "5 份样例齐备" if not missing else f"缺少：{'、'.join(missing)}",
        fix="uv run --project backend python sample_contracts/generate_samples.py",
    )
    report.add(
        "AP-004 附件缺失（AC16 的前提）",
        not (SAMPLES / MISSING_BY_DESIGN).is_file(),
        "确实不存在，符合预期" if not (SAMPLES / MISSING_BY_DESIGN).is_file() else "文件存在，AC16 无法演示",
        fix="删掉该文件（它是故意缺失的样例）",
        required=False,
    )
    report.add(
        "规则种子文件",
        (PROJECT_ROOT / "database" / "seed_rules.sql").is_file(),
        "database/seed_rules.sql",
        fix="仓库文件缺失，请重新检出",
    )


def check_ports(report: Report, env: dict[str, str]) -> None:
    # 8000 常被 Docker 的 relay 占着 IPv6，因此只作提醒
    for port, label, required in (
        (8000, "工具服务端口（Docker relay 也可能监听，正常）", False),
        (8100, "模拟审批系统端口", False),
        (5173, "调用端 dev server 端口", False),
    ):
        state = port_state(port)
        report.add(
            label,
            True,
            f"{port}：{'已被占用（可能是服务已在运行）' if state == 'listening' else '空闲'}",
            required=required,
        )

    db_host = env.get("DB_HOST", "127.0.0.1")
    db_port = int(env.get("DB_PORT", "3306") or 3306)
    with socket.socket() as sock:
        sock.settimeout(1.5)
        try:
            sock.connect((db_host, db_port))
            reachable = True
        except OSError:
            reachable = False
    report.add(
        "MySQL 可连接",
        reachable,
        f"{db_host}:{db_port}",
        fix="cd database; docker compose --env-file ../.env up -d",
    )


def describe_rule(report: Report) -> None:
    """尽量用项目自身的配置体系再确认一次（顺带验证 pydantic-settings 能加载）。"""
    sys.path.insert(0, str(PROJECT_ROOT / "backend"))
    try:
        from app.core.config import get_settings  # noqa: PLC0415

        settings = get_settings()
        report.add(
            "配置可被应用加载（fail-fast 预演）",
            True,
            f"env={settings.app_env}｜{settings.redacted_database_url()}｜LLM={'on' if settings.llm_enabled else 'off(降级)'}",
        )
    except Exception as exc:  # noqa: BLE001 - 预检必须给出结论而不是崩掉
        report.add(
            "配置可被应用加载（fail-fast 预演）",
            False,
            f"{type(exc).__name__}: {str(exc)[:160]}",
            fix="按提示补齐 .env 的必填项",
        )


def check_database(report: Report, env: dict[str, str]) -> None:
    if not env.get("DB_PASSWORD"):
        report.add("数据表与规则种子", False, "缺少 DB_PASSWORD，无法检查", fix="先补 .env")
        return
    try:
        import pymysql  # noqa: PLC0415
    except ImportError:
        report.add(
            "数据表与规则种子", False, "pymysql 不可用（请在 backend 环境内运行本脚本）", fix="uv run --project backend python scripts/preflight.py"
        )
        return

    try:
        connection = pymysql.connect(
            host=env.get("DB_HOST", "127.0.0.1"),
            port=int(env.get("DB_PORT", "3306") or 3306),
            user=env.get("DB_USER", "clauseguard"),
            password=env["DB_PASSWORD"],
            database=env.get("DB_NAME", "clauseguard"),
            connect_timeout=5,
            read_timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        report.add("数据表与规则种子", False, f"连接失败：{type(exc).__name__}: {str(exc)[:120]}", fix="确认容器 healthy 且 .env 口令与 compose 一致")
        return

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()"
            )
            tables = {row[0] for row in cursor.fetchall()}
            missing_tables = [name for name in EXPECTED_TABLES if name not in tables]
            report.add(
                "8 张业务表（DT-01…DT-08）",
                not missing_tables,
                "齐备" if not missing_tables else f"缺少：{'、'.join(missing_tables)}",
                fix="首次建表由 database/schema.sql 在容器初始化时执行；缺表可 down -v 后重建",
            )
            cursor.execute("SELECT COUNT(*) FROM review_rules WHERE rule_status = 'enabled'")
            enabled = int(cursor.fetchone()[0])
            report.add(
                "规则种子（R001–R011）",
                enabled >= 11,
                f"启用中的规则 {enabled} 条",
                fix="Get-Content database/seed_rules.sql -Raw | docker exec -i clauseguard-mysql mysql -uroot -p<口令>",
            )
    finally:
        connection.close()


# ── 输出 ───────────────────────────────────────────────────────────────────
def render(report: Report) -> None:
    icons = {"ok": OK, "warn": WARN, "bad": BAD}
    colors = {"ok": "\033[32m", "warn": "\033[33m", "bad": "\033[31m"}
    reset = "\033[0m"
    use_color = sys.stdout.isatty()

    print("=" * 88)
    print("ClauseGuard 环境预检（只读，不改任何配置）")
    print("=" * 88)
    for check in report.checks:
        icon = icons[check.level]
        line = f"  {icon} {check.name:<38} {check.detail}"
        print(f"{colors[check.level]}{line}{reset}" if use_color else line)
        if check.fix:
            print(f"        └─ 处理：{check.fix}")

    print("-" * 88)
    bad = report.failed
    if bad:
        print(f"结论：{len(bad)} 项必需条件未满足，先按上面的「处理」逐条补齐。")
    else:
        print("结论：必需条件全部满足，可以开始（命令见 README「快速开始」）。")
        print("提示：OCR 首次运行会联网下载模型（数百 MB），演示前先预热，见 docs/演示说明.md §0。")


def main() -> int:
    parser = argparse.ArgumentParser(description="ClauseGuard 环境预检")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（便于 CI 解析）")
    args = parser.parse_args()

    report = Report()
    check_host(report)
    env = check_config(report)
    check_assets(report)
    check_ports(report, env)
    describe_rule(report)
    check_database(report, env)

    if args.json:
        print(
            json.dumps(
                {
                    "passed": not report.failed,
                    "checks": [
                        {"name": item.name, "level": item.level, "detail": item.detail, "fix": item.fix}
                        for item in report.checks
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        render(report)
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
