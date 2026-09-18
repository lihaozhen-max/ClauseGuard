# ClauseGuard · 合同审批审查系统

企业合同审批流程中的"法务审查机器人"：自动拉取待审批合同 → 下载附件 → 解析（含 OCR）
→ 提取字段与条款 → 执行风险规则 → 汇总带原文证据的风险意见 → 保存结果 → 将中文评论写回审批系统评论区。

> **只提供风险识别、解释与审批关注点，不替代人工作出审批决定。**

## 文档索引

| 文档 | 编号 | 作用 |
|---|---|---|
| `合同审批审查系统 PRD.md` | — | 需求方权威（V1.0） |
| `docs/SPEC.md` | CG-SPEC-001 | **规范契约**：实现与验收以它为准（FR/IF/DT/ST/RL/PS/LM/CF/AC/TS） |
| `docs/技术方案与设计.md` | CG-DESIGN-001 | 实现手段（技术选型、目录、里程碑 M0–M7、风险登记册） |
| `docs/架构图.md` | CG-ARCH-001 | 13 张 Mermaid 图 + ASCII 版（架构、时序、状态机、ER、流水线） |
| `docs/接口说明.md` | CG-API-001 | 工具接口 / 内部 REST / 模拟审批系统接口的请求响应样例 |
| `docs/M0-验收记录.md` | CG-M0-001 | M0 环境与骨架的验证证据与决策记录 |
| `docs/M1-验收记录.md` | CG-M1-001 | M1 模拟审批系统 + 待办拉取 + 去重的验证证据与决策记录 |
| `docs/M2-验收记录.md` | CG-M2-001 | M2 附件下载 + 解析 + OCR + 字段提取的验证证据与决策记录 |
| `docs/M3-验收记录.md` | CG-M3-001 | M3 规则引擎 + 11 条规则 + 风险汇总 + 证据定位的验证证据与决策记录 |
| `docs/M4-验收记录.md` | CG-M4-001 | M4 摘要/关注点 + 结果入库 + 评论回写的验证证据与决策记录 |
| `docs/M5-验收记录.md` | CG-M5-001 | M5 异常阻塞 + 人工重试 + 全链路日志的验证证据与决策记录 |
| `docs/M6-验收记录.md` | CG-M6-001 | M6 前端五模块 + 规则维护 + 日志页的验证证据与决策记录 |

文档冲突时的裁决顺序：用户指令 → PRD → SPEC → 设计文档 → 通用工程习惯（SPEC §0.2）。

## 技术栈（已实测锁定）

| 项 | 值 |
|---|---|
| 后端 | Python **3.13.14**（uv 管理）+ FastAPI 0.141.1 + uvicorn 0.53.0 |
| 配置 | pydantic-settings 2.15.0（环境变量 / `.env`，fail-fast） |
| 数据 | MySQL 8（Docker Compose）+ SQLAlchemy 2.0.54 异步 + **asyncmy** 驱动 |
| 解析 | PyMuPDF / pdfplumber / python-docx + PaddleOCR（本地，M2） |
| LLM | DeepSeek 官方 API（`deepseek-flash`，OpenAI 兼容；可整体降级） |
| 前端 | Vue 3 + Element Plus + Vite 6 + TypeScript（调用端，M6） |

> Python 必须是 3.13：`paddlepaddle` 无 3.14 轮子（设计 R1，已实测确认）。

## 目录结构

```
ClauseGuard/
├── backend/                 工具服务（FastAPI）+ pyproject.toml + uv.lock + .venv
│   └── app/{core,db,modules,llm,clients,schemas,api,tools}
├── mock-approval-system/    模拟审批系统（独立进程，M1）
├── frontend-or-client/      调用端 Vue 3（M6）
├── database/                docker-compose.yml（MySQL 8）+ schema.sql（DT-01…DT-08）
├── sample_contracts/        自造样例合同（M1/M2）
├── docs/                    SPEC / 设计 / 架构图 / 里程碑记录
├── tests/                   单元 + 集成用例
├── screenshots/             验收截图（必须提交）
├── README.md
└── .env.example             配置模板（`.env` 不提交）
```

## 快速开始

### 1. 前置条件

- Python 3.13（推荐 uv 托管：`uv python install 3.13`）
- [uv](https://docs.astral.sh/uv/) ≥ 0.11
- Docker Desktop（含 Compose）

### 2. 配置

```powershell
cd ClauseGuard
Copy-Item .env.example .env
# 编辑 .env：至少填 DB_PASSWORD、DB_ROOT_PASSWORD、APPROVAL_API_KEY、INTERNAL_API_KEY
# LLM_API_KEY 可留空 —— 系统会自动降级为纯规则 + 模板摘要，全闭环仍可跑通（LM-18）
```

配置缺失时服务**启动即失败**并点名缺失项（CF-23），不会带病运行。

### 3. 启动数据库（MySQL 8）

```powershell
cd database
docker compose --env-file ../.env up -d
docker compose --env-file ../.env ps        # 等待 (healthy)
```

首次初始化会自动执行 `schema.sql` 建 8 张业务表、`seed_rules.sql` 灌入 R001–R011 规则。
需要重建库：`docker compose --env-file ../.env down -v` 后再 `up -d`。

> **库里已有数据时**（初始化脚本不会重跑），规则种子需手工灌一次：
>
> ```powershell
> Get-Content database/seed_rules.sql -Raw |
>   docker exec -i clauseguard-mysql mysql -uroot -p<DB_ROOT_PASSWORD> --default-character-set=utf8mb4
> ```
>
> 该脚本按 `rule_code` 幂等 upsert，可重复执行（会把规则改回基线）。

### 4. 安装依赖

```powershell
cd ../backend
uv sync
```

### 5. 启动模拟审批系统（独立进程，端口 8100）

```powershell
cd ../..
uv run --project backend uvicorn --app-dir mock-approval-system main:app --host 127.0.0.1 --port 8100
```

自检：`curl http://127.0.0.1:8100/health` —— 应显示 `approvals: 5`，并列出唯一故意缺失的附件。

### 6. 启动工具服务（端口 8000，另开一个终端）

```powershell
cd ClauseGuard/backend
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

验证：

```powershell
curl http://127.0.0.1:8000/health
# {"status":"ok", ..., "database":{"connected":true,"info":"MySQL 8.0.46 / charset=utf8mb4"}}

# 拉取待办（需要 X-API-Key，值取自 .env 的 INTERNAL_API_KEY）
curl -X POST http://127.0.0.1:8000/api/tasks/pull -H "X-API-Key: <INTERNAL_API_KEY>" -H "Content-Type: application/json" -d '{\"limit\":20}'
```

### 7. 跑测试

```powershell
cd ClauseGuard/backend
uv run pytest -q                          # 全量（含真实 OCR 推理 + 真实 LLM 冒烟，约 2~4 分钟）
uv run pytest -q -m "not slow and not llm"  # 跳过 OCR 与 LLM，约 15 秒
```

- 纯配置、字段抽取、规则判定用例无需数据库，也无需启动模拟审批系统；
- 集成用例在 MySQL 未就绪时**自动跳过**（不伪造通过）；
- 待办拉取用例通过 ASGI 内存传输直连模拟审批系统，不必先启动 8100 进程；
- 标记说明：`slow` = 真实 OCR 推理；`llm` = 真实 LLM 端点调用（无 Key 时自动跳过）。

### 8. 启动调用端（前端，端口 5173；另开一个终端）

```powershell
cd ClauseGuard/frontend-or-client
npm install
Copy-Item .env.example .env.local     # 填入 VITE_INTERNAL_API_KEY（= 后端 .env 的 INTERNAL_API_KEY）
npm run dev
```

浏览器打开 **http://127.0.0.1:5173**。前端通过 Vite 代理把 `/api` 转发到 `127.0.0.1:8000`，
因此**不需要**给后端加 CORS。若不想把密钥写进文件，也可以留空配置，
在页面右上角「接口密钥」对话框里临时填写（存在浏览器 localStorage，换后端无需重新构建）。

```powershell
npm run typecheck   # vue-tsc --noEmit
npm run build       # 产出 dist/
```

## 端口约定

| 端口 | 服务 |
|---|---|
| 8000 | 工具服务（`APP_PORT`） |
| 8100 | 模拟审批系统（`APPROVAL_BASE_URL`） |
| 5173 | Vite 开发服务器（仅开发期） |
| 3306 | MySQL 8（Docker Compose，仅绑定 127.0.0.1） |

## 鉴权与安全

- 调用端 → 工具服务的 `/api/**` 全部要求 `X-API-Key: {INTERNAL_API_KEY}`（缺失/错误 → 401）。
- 合同附件落盘到 `storage/contracts/<task_id>/`，**不挂任何静态路由**（FR-SYS-02）。
- 日志内置脱敏过滤器：数据库口令、审批系统 Key、内部 Key、LLM Key 一律抹除（FR-LOG-03）。
- `.env` 由 `.gitignore` 忽略，仓库中只保留 `.env.example`（CF-24）。

## 里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| **M0** | 环境搭建：uv + 3.13 虚拟环境、MySQL Compose、骨架与配置 | ✅ **已完成**（`docs/M0-验收记录.md`） |
| **M1** | 模拟审批系统 + 待办拉取 + 去重 | ✅ **已完成**（`docs/M1-验收记录.md`，AC01/AC02） |
| **M2** | 附件下载 + 解析（PDF/Word）+ OCR + 字段与条款提取 + 定位 | ✅ **已完成**（`docs/M2-验收记录.md`，AC03–AC07） |
| **M3** | 规则引擎 + 11 条规则 + 风险汇总 + 证据定位 | ✅ **已完成**（`docs/M3-验收记录.md`，AC08–AC10） |
| **M4** | 摘要/关注点 + 结果入库 + 评论生成与回写 | ✅ **已完成**（`docs/M4-验收记录.md`，AC11–AC15） |
| **M5** | `blocked` 状态 + 人工重试 + 全链路日志 | ✅ **已完成**（`docs/M5-验收记录.md`，AC16–AC18） |
| **M6** | 前端五个模块 + 规则维护（IF-21）+ 日志页 | ✅ **已完成**（`docs/M6-验收记录.md`，FR-UI-01…08、NF-10） |
| M7 | 闭环演示 + 截图 + 文档与交付整理 | ⏳ |

## 样例数据

`sample_contracts/` 下已有 5 份自造样例（SD-02），每份随附期望结果（SD-01，见 `expected_results.json`）：
AP-001/AP-002 文本型 PDF、AP-003 扫描件 PNG、AP-004 附件缺失、AP-005 空文件。
重新生成：`uv run --project backend python sample_contracts/generate_samples.py`。

## OCR 环境须知（重要）

扫描件识别走本地 PaddleOCR，有两个**必须满足**的环境条件（详见 `docs/M2-验收记录.md` §3.1/§3.2）：

1. **模型缓存目录必须是纯 ASCII 路径**——Paddle Inference 在 Windows 上打不开含中文的路径，
   会报出误导性的 `json parse error: attempting to parse an empty input`。
   因此 `OCR_MODEL_DIR` 默认留空 = 自动使用 `%LOCALAPPDATA%\ClauseGuard\ocr_models`；
   若显式配置了非 ASCII 路径，服务会**自动回退并打印告警**。
2. **必须关闭 oneDNN/MKLDNN**——paddle 3.3.1 的 PIR 执行器在该路径上有缺陷，
   `.env` 中 `OCR_ENABLE_MKLDNN=false` 请勿修改。

首次 OCR 会自动联网下载模型（数百 MB）；单页 A4 识别约 60s（CPU）。

## 规则与 LLM 须知

**规则**：R001–R011 存在 `review_rules` 表（种子见 `database/seed_rules.sql`），
只有 `rule_status=enabled` 的规则会被执行（FR-RULE-01）。阈值类规则的参数来自
`match_params_json`，缺省时回落到 `.env` 的 `RULE_PREPAY_MAX_RATIO` / `RULE_PAYMENT_MAX_DAYS`。
整体风险等级由 **RL-AGG** 算法决定，`uncertain` 与 `miss` 不参与计算（RL-AGG-01）。

**LLM**：只在**三处**出场（LM-01…LM-03）——R003/R004/R010 的语义判断、摘要生成、关注点生成；
**禁止**用它决定风险等级、生成证据原文或给出审批结论。要点：

- 端点/模型/Key 全部来自 `.env`（`LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY`），代码中无硬编码；
- `LLM_TEMPERATURE=0`、`LLM_MAX_TOKENS ≥ 512`（推理 token 计入配额，不足会导致
  **HTTP 200 但 content 为空**的静默失败，已显式防御）；
- 只解析 `message.content`；`reasoning_content` 仅入日志、**绝不回传**模型；
- 模型给出的原文片段**必须**通过"是否为合同原文连续子串"的回验（PS-10），
  回验失败即降级为 `uncertain`，绝不采信模型生成的文本；
- `LLM_ENABLED=false` 或 Key 留空时整体降级为"纯规则 + 模板摘要"，
  **AC01–AC19 全闭环仍可跑通**（LM-18）。

**评论回写**：审查结果按 §4.6.1 的规范模板渲染成中文评论，写回审批系统评论区。
幂等键为 `SHA-256(instance_id:review_id)` 并带数据库唯一索引——已成功回写过的记录再次调用
**直接返回既有结果**（`duplicate=true`）而不会再请求审批系统；回写失败**不会**删除审查结果，
任务也**不会**从 `done` 回退（FR-COM-04 / ST-01-04）。

一次完整审查的接口顺序：

```
POST /api/tasks/pull           ① 拉取待办（按 instance_id 去重）
POST /api/tasks/{id}/parse     ②③④⑤ 下载附件 + 解析 + 16 字段提取
POST /api/tasks/{id}/review    ⑥⑦⑧ 规则审查 + 摘要/关注点 + 评论生成 + 结果入库（→ done）
POST /api/tasks/{id}/write-comment  ⑨ 写回审批系统评论区（幂等）
GET  /api/tasks/{id}/logs       全链路日志（8 类核心操作，可按 log_type/level 过滤）
POST /api/tasks/{id}/retry      仅对 blocked 任务：从失败阶段重入并继续跑到 done
```

任一阶段不可恢复失败时任务进入 `blocked` 并记录 `blocked_stage` + `error_code`，
任务日志同时落库（`pull`/`download`/`parse`/`ocr`/`extract`/`rule`/`save`/`write_comment`/`retry`）；
排除故障后调 `retry` 即可续跑，`retry_count` 累加（ST-01-02 / ST-01-03 / AC16–AC18）。

## 调用端页面（M6）

| 路由 | 页面 | SPEC |
|---|---|---|
| `/tasks` | 待办调用：审批编号/标题/申请人/申请时间/附件数/任务状态 + 拉取、筛选、`blocked` 重试入口 | FR-UI-01 |
| `/tasks/{id}/detail` | 详情查看：审批基本信息、表单数据、合同附件（**仅元数据**，FR-SYS-02） | FR-UI-02 |
| `/tasks/{id}/parse` | 解析结果：基本信息 + 条款 + 原文片段 + 位置 + 提取状态（`missing`/`failed` 高亮） | FR-UI-03 |
| `/tasks/{id}/review` | 规则命中：总风险等级（三色）/风险数量/规则名/等级/证据/位置/建议 + 关注点 | FR-UI-04、NF-10 |
| `/tasks/{id}/result` | 结果处理：评论正文、回写状态/时间/错误 + 回写与重试按钮 | FR-UI-05、FR-UI-06 |
| `/tasks/{id}/logs` | 任务日志：8 类核心操作覆盖情况 + 全链路日志明细 | FR-UI-07、AC18 |
| `/rules` | 规则维护：启用/停用、改等级、改建议、新增规则 | FR-UI-08、IF-21 |

验收截图见 `screenshots/`。
