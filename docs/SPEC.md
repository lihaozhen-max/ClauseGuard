# ClauseGuard · 合同审批审查系统 — SPEC（规范书）

| 项 | 内容 |
|---|---|
| 文档编号 | CG-SPEC-001 |
| 版本 | v1.0 |
| 状态 | **规范性文档（Normative）**。实现与测试以本文档为准 |
| 上级依据 | `合同审批审查系统 PRD.md`（V1.0）；`vibecoding与微调项目实战.md` §1.3 |
| 同级文档 | `技术方案与设计.md`（CG-DESIGN-001，说明"如何实现"；本文档说明"必须实现成什么样"） |
| 生效日期 | 2026-09-17 |

---

## 0. 规范性说明

### 0.1 用语

本文档使用以下规范化用语，含义固定：

| 用语 | 含义 |
|---|---|
| **必须**（MUST） | 强制要求。不满足即为缺陷，验收不通过 |
| **禁止**（MUST NOT） | 强制禁止。出现即为缺陷 |
| **应当**（SHOULD） | 推荐要求。偏离时必须说明理由并记录 |
| **可以**（MAY） | 可选，不影响验收 |

### 0.2 文档优先级

发生冲突时，按以下顺序裁决：

```
① 用户当前明确的指令
② 本合同审批审查系统 PRD.md（需求方权威）
③ 本 SPEC（实现与验收契约）
④ 技术方案与设计.md（实现手段，可替换）
⑤ 通用工程习惯
```

本 SPEC 与 PRD 的差异**仅限**附录 A 列出的 5 处经确认偏离，其余内容必须与 PRD 一致。

### 0.3 需求编号规则

| 前缀 | 含义 |
|---|---|
| `FR-xxx-nn` | 功能需求 |
| `IF-nn` | 接口规格 |
| `DT-nn` | 数据表规格 |
| `ST-nn` | 状态机规格 |
| `RL-nnn` | 规则规格 |
| `PS-nn` | 解析规格 |
| `LM-nn` | LLM 规格 |
| `CF-nn` | 配置规格 |
| `NF-nn` | 非功能规格 |
| `AC-nn` | 验收标准（对应 PRD 第 22 节 AC01–AC19） |
| `TS-nn` | 测试场景 |
| `OPEN-nn` | 未决事项 |

**每个 `AC` 必须能追溯到至少一个 `FR`/`IF`/`ST`/`RL`；每个 `FR` 必须至少被一个 `TS` 覆盖。**

---

## 1. 术语

| 术语 | 定义 |
|---|---|
| 审批实例 | 审批系统中的一个合同审批单，由 `instance_id` 唯一标识 |
| 任务 | 本系统中一次合同审查工作单元，主键 `task_id` |
| 附件 | 审批单下挂的合同文件 |
| 合同文本 | 解析清洗后的纯文本（含页码映射） |
| 条款 | 合同中一个语义段落（付款、验收……） |
| 字段记录 | 一个被提取的结构化字段（见 §3.4） |
| 命中 | 一条风险规则判定为"存在风险" |
| 证据 | 支撑命中的**合同原文片段**（禁止由模型生成） |
| 阻塞 | 任务进入 `blocked` 状态，等待人工重试 |

---

## 2. 全局约定

### 2.1 标识符与命名映射

| 名称 | 含义 | 载体 |
|---|---|---|
| `instance_id` | 审批实例编号，**唯一业务标识** | 审批系统提供 |
| `task_id` | 本系统任务主键 | `approval_tasks.id` |
| **`case_id`** | **≡ `task_id`**（PRD 接口签名中的 `case_id`，内部统一映射） | 同 `task_id` |
| `attachment_id` | 审批系统侧附件编号 | `approval_attachments.attachment_code` |
| `document_id` | 解析记录主键 | `contract_parses.id` |
| `review_id` | 审查结果主键 | `review_results.id` |
| `rule_id` | 规则主键 | `review_rules.id` |

> **IF-05 `run_contract_rules(case_id)` 与 IF-06 `save_review_result(case_id, ...)` 的 `case_id` 参数，一律解析为 `approval_tasks.id`。** 接口签名保持 PRD 原样不变。

### 2.2 枚举取值（**禁止扩展，禁止大小写变体**）

| 枚举 | 取值 |
|---|---|
| `task_status` | `pending` `parsing` `reviewing` `blocked` `done` |
| `write_status` | `not_written` `writing` `success` `failed` |
| `risk_level` | `low` `medium` `high` |
| `rule_status` | `enabled` `disabled` |
| `match_mode` | `regex` `keyword` `threshold` `presence` `llm_semantic` |
| `extract_status` | `success` `missing` `failed` |
| `parse_status` | `success` `partial` `failed` |
| `parse_mode` | `text` `ocr` |
| `hit_status` | `hit` `miss` `uncertain` |
| `hit_source` | `rule` `llm` |
| `download_status` | `pending` `success` `failed` |
| `log_level` | `info` `warning` `error` |
| `log_type` | `pull` `download` `parse` `ocr` `extract` `rule` `save` `write_comment` `retry` |

**整体风险等级以中文返回给用户时映射为：`low→低`、`medium→中`、`high→高`；接口 JSON 中一律使用英文枚举值。**

### 2.3 时间与字符集

- 数据库：统一字符集 `utf8mb4` / 排序规则 `utf8mb4_general_ci`；时间列类型 `DATETIME`，**存 UTC**。
- 接口：时间字段一律 ISO 8601 UTC 字符串，格式 `YYYY-MM-DDTHH:MM:SSZ`。
- `field_value` 中的日期：`YYYY-MM-DD`；金额：**纯数字字符串，不含千分位与货币符号**（如 `500000`）；币种：ISO 4217 三字母大写（如 `CNY`）。
- 所有面向用户的文本（摘要、建议、评论）必须为简体中文。

### 2.4 字段记录（FieldRecord）

`FR-PARSE-06`：**每一个**提取字段必须表示为下列 5 键对象，缺一不可：

```json
{
  "field_name": "contract_amount",
  "field_value": "500000",
  "source_text": "本合同总金额为人民币伍拾万元整",
  "position": "第2页 第3段",
  "extract_status": "success"
}
```

| 键 | 类型 | 约束 |
|---|---|---|
| `field_name` | string | 必须取自 §2.5 的固定枚举 |
| `field_value` | string \| null | `extract_status != success` 时可以为 `null` |
| `source_text` | string \| null | **必须是合同原文的连续子串**；`success` 时禁止为 `null` |
| `position` | string | 见 §2.6；`missing` 时为 `null` |
| `extract_status` | enum | `success` / `missing` / `failed` |

> `success` 判定：`field_value` 非空 **且** `source_text` 是 `full_text` 的连续子串 **且** `position` 非空。三者缺一即降级为 `failed`，并记录降级原因（`[PRD 4.3]`）。

### 2.5 字段名枚举（16 个，固定）

| 分组 | `field_name` | 中文名 |
|---|---|---|
| 基本信息 | `contract_title` | 合同标题 |
| | `contract_no` | 合同编号 |
| | `party_a` | 签约主体 |
| | `party_b` | 对方名称 |
| | `contract_amount` | 合同金额 |
| | `currency` | 币种 |
| | `effective_date` | 生效时间 |
| | `expiry_date` | 到期时间 |
| 条款 | `payment_clause` | 付款条款 |
| | `delivery_clause` | 交付条款 |
| | `acceptance_clause` | 验收条款 |
| | `breach_clause` | 违约条款 |
| | `confidentiality_clause` | 保密条款 |
| | `data_clause` | 数据条款 |
| | `ip_clause` | 知识产权条款 |
| | `dispute_clause` | 争议解决条款 |

### 2.6 `position` 格式（**两种，按 parse_mode 二选一**）

| `parse_mode` | 格式 | 正则 | 示例 |
|---|---|---|---|
| `text` | `第{页}页 第{段}段` | `^第\d+页 第\d+段$` | `第4页 第3段` |
| `ocr` | `第{页}页 区域({x},{y})` | `^第\d+页 区域\(\d+,\d+\)$` | `第1页 区域(120,480)` |

- 页码从 **1** 开始计数；段号从 **1** 开始，按解析后的段落顺序。
- OCR 坐标 `(x, y)` 为文本框左上角像素坐标，取整。
- **禁止**出现第三种格式；**禁止**在 `text` 模式下使用坐标。

---

## 3. 系统边界

```
┌──────────────────┐  HTTP   ┌────────────────────────┐
│ 模拟审批系统       │◄───────►│ ClauseGuard 工具服务    │
│ （外部系统角色）   │         │  FastAPI               │
└──────────────────┘         └───────┬────────────────┘
                                     │
              ┌──────────────────────┼───────────────────────┐
              ▼                      ▼                       ▼
        MySQL 8              本地文件存储              DeepSeek API
      （8 张业务表）      （合同附件，不公开）      （语义规则+摘要）
```

- **FR-SYS-01**：模拟审批系统**必须**作为独立进程运行，工具服务**必须**仅通过 HTTP 访问它。禁止直接读写其数据库或内存。
- **FR-SYS-02**：合同附件**禁止**通过任何静态路由对外暴露；**必须**仅由后端按权限读取。
- **FR-SYS-03**：工具服务与调用端之间的内部接口**必须**校验 `X-API-Key`（值来自 `INTERNAL_API_KEY`）。

---

## 4. 功能规格

### 4.1 审批接入（Approval）

| 编号 | 要求 |
|---|---|
| FR-APP-01 | **必须**支持按 `limit` 拉取待处理审批单，字段至少含 `instance_id`、`approval_code`、`approval_title`、`applicant_name`、`apply_time`、`attachment_count`、`current_status` |
| FR-APP-02 | **必须**按 `instance_id` 去重：已存在则**更新**已有任务信息，**禁止**新建第二个任务 |
| FR-APP-03 | 去重**必须以数据库唯一索引为最终保障**，禁止仅依赖应用层"先查后插"（并发下会失效） |
| FR-APP-04 | **必须**支持按 `instance_id` 获取审批详情：审批信息、表单字段、合同类型、当前状态、附件列表 |
| FR-APP-05 | 重复拉取时**禁止**重置 `task_status`（已 `done` 的任务不得被拉回 `pending`） |
| FR-APP-06 | 审批接口调用失败**必须**记 `APPROVAL_API_ERROR` 并支持重试 |

### 4.2 附件（Attachment）

| 编号 | 要求 |
|---|---|
| FR-ATT-01 | **必须**按 `instance_id` + `attachment_id` 下载附件，落盘到 `STORAGE_DIR` |
| FR-ATT-02 | **必须**记录 `file_name`、`file_type`、`file_path`、`file_size`、`file_checksum`(SHA-256)、`download_status` |
| FR-ATT-03 | 附件**必须**落盘到以 `task_id` 分隔的子目录，文件名**必须**做路径穿越（`..`／绝对路径）消毒 |
| FR-ATT-04 | 附件不存在 → `CONTRACT_ATTACHMENT_MISSING`，任务转 `blocked`（`blocked_stage=parsing`） |
| FR-ATT-05 | 下载失败 → `DOWNLOAD_FAILED`，**必须**记录请求信息、返回状态、错误信息 |
| FR-ATT-06 | 重复下载同一附件**应当**覆盖并更新记录，不产生第二条附件记录（`(task_id, attachment_code)` 唯一） |

### 4.3 解析（Parser）

| 编号 | 要求 |
|---|---|
| FR-PARSE-01 | **必须**支持 `.pdf`、`.docx`、图片（`.png`/`.jpg`/`.jpeg`）三类输入 |
| FR-PARSE-02 | PDF **必须**区分文本型与扫描型：单页可提取有效字符数低于阈值即判定该页为扫描页，走 OCR |
| FR-PARSE-03 | 扫描件**必须**经 OCR 得到文本与文本框坐标 |
| FR-PARSE-04 | 解析后**必须**产出统一合同文本（`full_text` + `page_map_json` 页码映射） |
| FR-PARSE-05 | **必须**提取 §2.5 全部 16 个字段，每个字段一条 FieldRecord（§2.4） |
| FR-PARSE-06 | 条款字段的 `source_text` **必须**为条款原文（可截断至 ≤ 2000 字符，截断处加 `…`） |
| FR-PARSE-07 | 解析失败**必须**写入 `parse_error` 并置 `parse_status=failed`；**禁止**仅返回空结果 `[PRD 13]` |
| FR-PARSE-08 | 内容为空 → `EMPTY_CONTRACT_CONTENT`；OCR 失败 → `OCR_FAILED`；两者均转 `blocked`（`blocked_stage=parsing`） |
| FR-PARSE-09 | 重复解析同一附件**必须**更新原记录（`(task_id, attachment_id)` 唯一），不新增 |
| FR-PARSE-10 | 任一字段提取失败**禁止**中断整体解析；该字段标 `failed`，其余字段照常产出，`parse_status=partial` |

### 4.4 规则引擎（Rule Engine）

| 编号 | 要求 |
|---|---|
| FR-RULE-01 | **必须**从 `review_rules` 加载 `rule_status=enabled` 的规则 |
| FR-RULE-02 | **必须**支持 §2.2 的 5 种 `match_mode` |
| FR-RULE-03 | 每条规则**必须**输出 `hit_status`（`hit`/`miss`/`uncertain`）与 `hit_source`（`rule`/`llm`） |
| FR-RULE-04 | 命中时**必须**提供 `evidence_text`（合同原文连续子串）与 `evidence_position`（§2.6 格式） |
| FR-RULE-05 | 证据文本**必须**由系统从合同文本中截取，**禁止**采用 LLM 生成的文本作为证据 |
| FR-RULE-06 | 单条规则执行异常**禁止**中断其他规则；该规则记 `uncertain` + 日志 |
| FR-RULE-07 | 重复执行审查**必须**覆盖旧命中（`(task_id, rule_id)` 唯一），不产生重复命中 |
| FR-RULE-08 | 阈值类规则**必须**从 `match_params_json` 读取参数；缺失时回落 `[CF]` 配置默认值 |
| FR-RULE-09 | 命中记录**必须**快照当时的 `risk_level` 与 `suggestion_text`，防止规则后续修改导致历史结果漂移 |
| FR-RULE-10 | 全部规则**必须**实现 §9 的 RL-001…RL-011 共 11 条 |
| FR-RULE-11 | 规则执行阶段异常 → 任务转 `blocked`（`blocked_stage=reviewing`） |

### 4.5 审查汇总（Review）

| 编号 | 要求 |
|---|---|
| FR-REV-01 | **必须**按 RL-AGG（§9.12）计算 `overall_risk_level` |
| FR-REV-02 | **必须**生成中文 `summary_text` |
| FR-REV-03 | **必须**生成 `focus_points` 数组（审批关注点） |
| FR-REV-04 | **必须**生成 `comment_text`（§4.6 模板） |
| FR-REV-05 | 结果**必须**按 `task_id` 唯一保存（upsert） |
| FR-REV-06 | `uncertain` 命中**禁止**计入整体风险等级，但**必须**出现在结果中供人工确认 |
| FR-REV-07 | `summary_text` / `focus_points` 生成失败时**必须**降级为模板生成，任务**禁止**因此转 `blocked` |

### 4.6 评论回写（Comment）

| 编号 | 要求 |
|---|---|
| FR-COM-01 | 评论内容**必须**遵循 §4.6.1 模板 |
| FR-COM-02 | 回写**必须**幂等：同一 `(instance_id, review_id)` 重复调用**禁止**产生第二条评论 |
| FR-COM-03 | 回写过程**必须**记 `comment_logs`（`write_status`、返回原文、错误信息） |
| FR-COM-04 | 回写失败**禁止**删除已产生的审查结果，**仅**置 `write_status=failed` `[PRD 13]` |
| FR-COM-05 | 管理员**必须**可重新触发回写 |
| FR-COM-06 | 回写结果为已 `success` 时再次调用，**必须**直接返回既有结果并标记 `duplicate=true`，不重复请求审批系统 |
| FR-COM-07 | 评论正文**必须**以免责声明结尾（见 §4.6.1），**禁止**出现"审批通过/不通过"等替代人工决策的表述 |

#### 4.6.1 评论模板（**规范文本，禁止改动措辞**）

**存在命中（`overall_risk_level` 为 medium/high）时：**

```
【合同自动审查结果】

整体风险等级：{高|中}

风险摘要：
{summary_text}

重点关注：

1. {focus_point_1}
证据：{evidence_position}。

2. {focus_point_2}
建议：{suggestion}。

以上结果由合同审查系统自动生成，仅供审批人员参考，最终审批结论由审批人员判断。
```

**无命中（`low`）时：**

```
【合同自动审查结果】

整体风险等级：低

本次自动审查未发现明确的高风险或中风险条款。

以上结果由合同审查系统自动生成，仅供审批人员参考，最终审批结论由审批人员判断。
```

**约束：**
- 风险等级**必须**输出中文（高/中/低）。
- 关注点**必须**编号，从 1 开始，最多 5 条。
- 关注点为风险项时**必须**附 `证据：{evidence_position}。`；为缺失项（无原文可定位）时**必须**改为附 `建议：{suggestion}。`。
- `evidence_position` **禁止**为空；为空时该条关注点改用建议行。

### 4.7 日志（Logging）

| 编号 | 要求 |
|---|---|
| FR-LOG-01 | 下列 8 类核心操作**必须**落 `task_logs`：待办获取、附件下载、文档解析、OCR、字段提取、规则执行、结果保存、评论回写 `[PRD 19.4]` |
| FR-LOG-02 | 每条日志**必须**含 `task_id`、`log_level`、`log_type`、`log_content`、`created_at` |
| FR-LOG-03 | **禁止**在日志中记录 API Key、Token、密码、数据库连接串 |
| FR-LOG-04 | 日志中的合同原文**应当**截断至 ≤ 500 字符 |
| FR-LOG-05 | 人工重试**必须**记 `log_type=retry` 日志 |

### 4.8 调用端（UI）

| 编号 | 要求 |
|---|---|
| FR-UI-01 | 待办调用模块：展示审批编号、标题、申请人、申请时间、附件数量、任务状态 |
| FR-UI-02 | 详情查看模块：展示审批基本信息、表单数据、合同附件 |
| FR-UI-03 | 解析结果模块：展示合同基本信息与条款，含原文片段、页码/位置、提取状态；`missing`/`failed` 必须**视觉区分** |
| FR-UI-04 | 规则命中模块：展示总风险等级、风险数量、规则名、风险等级、证据、位置、建议 |
| FR-UI-05 | 结果处理模块：展示评论内容、回写状态、回写时间、回写错误；提供回写/重试操作 |
| FR-UI-06 | **必须**提供 `blocked` 任务的人工重试入口 |
| FR-UI-07 | **必须**提供任务日志查看页 |
| FR-UI-08 | **应当**提供规则维护页（启用/停用、改等级、改建议） |

### 4.9 模拟审批系统（Mock）

| 编号 | 要求 |
|---|---|
| FR-MOCK-01 | **必须**提供 4 个接口：待办列表、审批详情、附件下载、评论写入 |
| FR-MOCK-02 | 评论写入接口**必须**按 `idempotency_key` 去重并返回 `remark_id` |
| FR-MOCK-03 | **必须**内置 §16 的 5 个样例审批单 |
| FR-MOCK-04 | 附件下载接口**必须**能返回真实文件流，且能模拟"附件不存在"（HTTP 404） |

---

## 5. 接口规格

### 5.1 工具接口（7 个，签名与 PRD 一致，**禁止改名改参数**）

#### IF-01 `list_pending_contract_approvals(limit)`

**入参**：`limit: int`（≥1；缺省 20）

**出参**：

```json
{
  "items": [
    {
      "instance_id": "AP-001",
      "approval_code": "CG-2026-0001",
      "approval_title": "服务器采购合同审批",
      "applicant_name": "张三",
      "apply_time": "2026-09-10T02:30:00Z",
      "attachment_count": 1,
      "current_status": "pending",
      "task_id": 1,
      "task_status": "pending",
      "dedup": "created"
    }
  ],
  "created_count": 1,
  "updated_count": 0
}
```

**约束**：`dedup` ∈ `created` / `updated`。同一 `instance_id` 二次调用**必须**返回 `updated` 且 `task_id` 不变。

---

#### IF-02 `get_contract_approval(instance_id)`

**出参**：

```json
{
  "instance_id": "AP-001",
  "approval_code": "CG-2026-0001",
  "approval_title": "服务器采购合同审批",
  "applicant_name": "张三",
  "apply_time": "2026-09-10T02:30:00Z",
  "current_status": "pending",
  "contract_type": "采购合同",
  "form_data": { "amount": "500000", "supplier": "某某科技有限公司" },
  "attachments": [
    { "attachment_id": "ATT-001", "file_name": "采购合同.pdf", "file_type": "pdf", "file_size": 248163 }
  ],
  "task_id": 1,
  "task_status": "pending"
}
```

**异常**：`instance_id` 不存在 → 404 + `APPROVAL_NOT_FOUND`。

---

#### IF-03 `download_contract_attachment(instance_id, attachment_id, file_name)`

**出参**：

```json
{
  "task_id": 1,
  "attachment_id": "ATT-001",
  "file_name": "采购合同.pdf",
  "file_type": "pdf",
  "file_path": "./storage/contracts/1/ATT-001_采购合同.pdf",
  "file_size": 248163,
  "file_checksum": "9f2c…",
  "download_status": "success"
}
```

**异常**：附件不存在 → `CONTRACT_ATTACHMENT_MISSING` + 任务 `blocked`；下载失败 → `DOWNLOAD_FAILED` + 任务 `blocked`。

---

#### IF-04 `parse_contract_document(document_id)`

**出参**：

```json
{
  "document_id": 1,
  "task_id": 1,
  "parse_mode": "text",
  "parse_status": "success",
  "basic_info": [ /* 8 条 FieldRecord */ ],
  "clause_info": [ /* 8 条 FieldRecord */ ],
  "parse_error": null
}
```

**约束**：`basic_info` 与 `clause_info` **必须**各含 §2.5 全部条目（缺失者以 `extract_status=missing` 呈现），**禁止**缺项。

---

#### IF-05 `run_contract_rules(case_id)`

**出参**：

```json
{
  "case_id": 1,
  "overall_risk_level": "high",
  "hit_count": 4,
  "uncertain_count": 1,
  "rule_hits": [
    {
      "rule_code": "R001",
      "rule_name": "预付款比例风险",
      "risk_level": "high",
      "hit_status": "hit",
      "hit_source": "rule",
      "evidence_text": "合同签订后三日内支付合同总额80%",
      "evidence_position": "第4页 第3段",
      "suggestion": "建议降低预付款比例或增加履约保障措施"
    }
  ],
  "summary_text": "本合同存在较高预付款比例、自动续约、验收标准缺失及管辖地不利等风险。",
  "focus_points": [
    "确认80%预付款是否符合公司付款政策",
    "补充明确的交付验收标准"
  ]
}
```

**约束**：`case_id` 不存在 → 404；无审查结果（未解析）→ 409 + `PARSE_REQUIRED`。

---

#### IF-06 `save_review_result(case_id, overall_risk_level, summary_text, focus_points_json, comment_text)`

**出参**：`{ "review_id": 1, "task_id": 1, "saved": true }`

**约束**：按 `task_id` upsert；重复保存**必须**更新同一 `review_id`。

---

#### IF-07 `write_approval_comment(instance_id, review_id)`

**出参**：

```json
{
  "task_id": 1,
  "review_id": 1,
  "write_status": "success",
  "remark_id": "RMK-88001",
  "write_response_text": "{\"code\":0,\"remark_id\":\"RMK-88001\"}",
  "duplicate": false
}
```

**异常**：回写失败 → `write_status=failed`，任务状态**保持 `done`**，附 `COMMENT_WRITE_FAILED`。

---

### 5.2 内部 REST（调用端 → 工具服务）

**鉴权**：全部**必须**带请求头 `X-API-Key: {INTERNAL_API_KEY}`；缺失或不匹配 → 401。

| 编号 | 方法 | 路径 | 用途 |
|---|---|---|---|
| IF-10 | `POST` | `/api/tasks/pull` | 触发拉取（body `{"limit":20}`） |
| IF-11 | `GET` | `/api/tasks?status=&page=&size=` | 任务列表 |
| IF-12 | `GET` | `/api/tasks/{task_id}` | 任务详情（含附件） |
| IF-13 | `GET` | `/api/tasks/{task_id}/parse` | 解析结果 |
| IF-14 | `POST` | `/api/tasks/{task_id}/parse` | 触发解析 |
| IF-15 | `GET` | `/api/tasks/{task_id}/review` | 审查结果 |
| IF-16 | `POST` | `/api/tasks/{task_id}/review` | 触发规则审查 |
| IF-17 | `POST` | `/api/tasks/{task_id}/write-comment` | 触发/重试回写 |
| IF-18 | `GET` | `/api/tasks/{task_id}/comment-logs` | 回写历史 |
| IF-19 | `GET` | `/api/tasks/{task_id}/logs` | 任务日志 |
| IF-20 | `POST` | `/api/tasks/{task_id}/retry` | 人工重试 `blocked` 任务 |
| IF-21 | `GET`/`POST`/`PUT` | `/api/rules` | 规则查询/新增/修改 |

### 5.3 错误响应统一结构

```json
{
  "error": {
    "code": "CONTRACT_ATTACHMENT_MISSING",
    "message": "审批单 AP-004 未提供合同附件",
    "task_id": 4,
    "detail": {}
  }
}
```

### 5.4 错误码总表（**规范**）

| 错误码 | 触发条件 | 任务状态 | 回写状态 |
|---|---|---|---|
| `APPROVAL_NOT_FOUND` | 审批实例不存在 | 不变 | 不变 |
| `APPROVAL_API_ERROR` | 审批接口异常 | `blocked`（当前阶段） | 不变 |
| `CONTRACT_ATTACHMENT_MISSING` | 附件不存在 | `blocked` / `parsing` | 不变 |
| `DOWNLOAD_FAILED` | 附件下载失败 | `blocked` / `parsing` | 不变 |
| `EMPTY_CONTRACT_CONTENT` | 合同内容为空 | `blocked` / `parsing` | 不变 |
| `OCR_FAILED` | OCR 识别失败 | `blocked` / `parsing` | 不变 |
| `PARSE_FAILED` | 解析失败 | `blocked` / `parsing` | 不变 |
| `PARSE_REQUIRED` | 未解析即请求审查 | 不变 | 不变 |
| `RULE_EXECUTION_FAILED` | 规则引擎异常 | `blocked` / `reviewing` | 不变 |
| `LLM_FAILED` | LLM 调用失败 | **不变**（降级处理） | 不变 |
| `COMMENT_WRITE_FAILED` | 评论回写失败 | **保持 `done`** | `failed` |
| `UNAUTHORIZED` | 缺少/错误 `X-API-Key` | 不变 | 不变 |

---

## 6. 数据规格

**通用要求**：
- **DT-00-01** 所有表**必须**有主键 `id`（`BIGINT AUTO_INCREMENT`）。
- **DT-00-02** 所有表字符集**必须**为 `utf8mb4`。
- **DT-00-03** 时间列**必须**为 `DATETIME`（存 UTC）。
- **DT-00-04** 外键**必须**为 `ON DELETE RESTRICT`（审查结果与日志**禁止**被级联删除）。
- **DT-00-05** 唯一索引是幂等性的**最终保障**，见下表标注 ⭐ 的约束。

### DT-01 `approval_tasks`

| 字段 | 类型 | 空 | 默认 | 约束/说明 |
|---|---|---|---|---|
| `id` | BIGINT | 否 | AI | PK，即 `task_id`/`case_id` |
| `instance_id` | VARCHAR(64) | 否 | — | ⭐**UNIQUE**，唯一业务标识与去重键 |
| `approval_code` | VARCHAR(64) | 否 | — | 审批编号 |
| `approval_title` | VARCHAR(255) | 否 | — | |
| `applicant_name` | VARCHAR(64) | 否 | — | |
| `apply_time` | DATETIME | 是 | NULL | 申请时间 |
| `attachment_count` | INT | 否 | 0 | |
| `current_status` | VARCHAR(32) | 是 | NULL | 审批系统侧状态 |
| `contract_type` | VARCHAR(64) | 是 | NULL | |
| `form_data_json` | JSON | 是 | NULL | 审批表单字段 |
| `task_status` | VARCHAR(16) | 否 | `pending` | §2.2 枚举 |
| `write_status` | VARCHAR(16) | 否 | `not_written` | §2.2 枚举 |
| `blocked_stage` | VARCHAR(16) | 是 | NULL | `parsing` / `reviewing` |
| `error_code` | VARCHAR(64) | 是 | NULL | §5.4 |
| `error_message` | TEXT | 是 | NULL | |
| `retry_count` | INT | 否 | 0 | |
| `created_at` | DATETIME | 否 | — | |
| `updated_at` | DATETIME | 否 | — | |

索引：`UNIQUE(instance_id)`、`INDEX(task_status)`、`INDEX(write_status)`。

### DT-02 `approval_attachments`

| 字段 | 类型 | 空 | 默认 | 约束/说明 |
|---|---|---|---|---|
| `id` | BIGINT | 否 | AI | PK |
| `task_id` | BIGINT | 否 | — | FK → DT-01.id |
| `attachment_code` | VARCHAR(64) | 否 | — | 审批系统侧附件编号，即 `attachment_id` |
| `file_name` | VARCHAR(255) | 否 | — | |
| `file_type` | VARCHAR(16) | 否 | — | `pdf`/`docx`/`png`/`jpg`/`jpeg` |
| `file_path` | VARCHAR(512) | 是 | NULL | 本地路径，**禁止**对外暴露 |
| `file_size` | BIGINT | 是 | NULL | 字节 |
| `file_checksum` | VARCHAR(64) | 是 | NULL | SHA-256 十六进制 |
| `download_status` | VARCHAR(16) | 否 | `pending` | §2.2 枚举 |
| `created_at` | DATETIME | 否 | — | |

索引：⭐`UNIQUE(task_id, attachment_code)`、`INDEX(task_id)`。

### DT-03 `contract_parses`

| 字段 | 类型 | 空 | 默认 | 约束/说明 |
|---|---|---|---|---|
| `id` | BIGINT | 否 | AI | PK，即 `document_id` |
| `task_id` | BIGINT | 否 | — | FK → DT-01.id |
| `attachment_id` | BIGINT | 否 | — | FK → DT-02.id |
| `basic_info_json` | JSON | 否 | — | FieldRecord 数组，8 条 |
| `clause_info_json` | JSON | 否 | — | FieldRecord 数组，8 条 |
| `full_text` | LONGTEXT | 是 | NULL | 清洗后全文（证据校验基准） |
| `page_map_json` | JSON | 是 | NULL | 页码 → 文本偏移 |
| `parse_mode` | VARCHAR(16) | 否 | `text` | `text`/`ocr`，决定 position 格式 |
| `parse_status` | VARCHAR(16) | 否 | — | `success`/`partial`/`failed` |
| `parse_error` | TEXT | 是 | NULL | 失败原因 |
| `created_at` | DATETIME | 否 | — | |

索引：⭐`UNIQUE(task_id, attachment_id)`、`INDEX(task_id)`。

### DT-04 `review_rules`

| 字段 | 类型 | 空 | 默认 | 约束/说明 |
|---|---|---|---|---|
| `id` | BIGINT | 否 | AI | PK，即 `rule_id` |
| `rule_code` | VARCHAR(16) | 否 | — | ⭐**UNIQUE**，`R001`…`R011` |
| `rule_name` | VARCHAR(128) | 否 | — | |
| `risk_level` | VARCHAR(8) | 否 | — | `low`/`medium`/`high` |
| `rule_status` | VARCHAR(8) | 否 | `enabled` | `enabled`/`disabled` |
| `match_mode` | VARCHAR(16) | 否 | — | §2.2 枚举 |
| `match_text` | TEXT | 是 | NULL | 正则/关键词/表达式载体 |
| `match_params_json` | JSON | 是 | NULL | 阈值等参数，如 `{"max_ratio":0.30}` |
| `suggestion_text` | TEXT | 否 | — | 处理建议 |
| `target_section` | VARCHAR(32) | 是 | NULL | 限定匹配部位，如 `payment` |
| `updated_at` | DATETIME | 否 | — | |

### DT-05 `rule_hits`

| 字段 | 类型 | 空 | 默认 | 约束/说明 |
|---|---|---|---|---|
| `id` | BIGINT | 否 | AI | PK |
| `task_id` | BIGINT | 否 | — | FK → DT-01.id |
| `rule_id` | BIGINT | 否 | — | FK → DT-04.id |
| `risk_level` | VARCHAR(8) | 否 | — | 命中时快照 |
| `evidence_text` | TEXT | 是 | NULL | **合同原文连续子串** |
| `evidence_position` | VARCHAR(128) | 是 | NULL | §2.6 格式 |
| `suggestion_text` | TEXT | 是 | NULL | 命中时快照 |
| `hit_source` | VARCHAR(16) | 否 | `rule` | `rule`/`llm` |
| `hit_status` | VARCHAR(16) | 否 | — | `hit`/`miss`/`uncertain` |
| `created_at` | DATETIME | 否 | — | |

索引：⭐`UNIQUE(task_id, rule_id)`、`INDEX(task_id)`。

### DT-06 `review_results`

| 字段 | 类型 | 空 | 默认 | 约束/说明 |
|---|---|---|---|---|
| `id` | BIGINT | 否 | AI | PK，即 `review_id` |
| `task_id` | BIGINT | 否 | — | ⭐**UNIQUE**，FK → DT-01.id |
| `overall_risk_level` | VARCHAR(8) | 否 | — | |
| `summary_text` | TEXT | 否 | — | |
| `focus_points_json` | JSON | 否 | — | string 数组 |
| `comment_text` | TEXT | 否 | — | §4.6.1 模板产物 |
| `created_at` | DATETIME | 否 | — | |

### DT-07 `comment_logs`

| 字段 | 类型 | 空 | 默认 | 约束/说明 |
|---|---|---|---|---|
| `id` | BIGINT | 否 | AI | PK |
| `task_id` | BIGINT | 否 | — | FK → DT-01.id |
| `write_status` | VARCHAR(16) | 否 | — | `writing`/`success`/`failed` |
| `write_response_text` | TEXT | 是 | NULL | 审批系统返回原文/错误 |
| `remark_id` | VARCHAR(64) | 是 | NULL | 审批系统评论 ID |
| `idempotency_key` | VARCHAR(64) | 否 | — | ⭐**UNIQUE**，`instance_id + ':' + review_id` 的 SHA-256 |
| `created_at` | DATETIME | 否 | — | |

### DT-08 `task_logs`

| 字段 | 类型 | 空 | 默认 | 约束/说明 |
|---|---|---|---|---|
| `id` | BIGINT | 否 | AI | PK |
| `task_id` | BIGINT | 否 | — | FK → DT-01.id |
| `log_level` | VARCHAR(8) | 否 | — | `info`/`warning`/`error` |
| `log_type` | VARCHAR(32) | 否 | — | §2.2 枚举 |
| `log_content` | TEXT | 否 | — | **已脱敏** |
| `created_at` | DATETIME | 否 | — | |

索引：`INDEX(task_id, created_at)`。

---

## 7. 状态机规格

### ST-01 任务状态

**合法迁移（**仅这些**，其余一律禁止）：**

| 从 | 到 | 触发条件 |
|---|---|---|
| — | `pending` | 待办拉取创建任务 |
| `pending` | `parsing` | 开始下载/解析 |
| `parsing` | `reviewing` | 解析成功 |
| `reviewing` | `done` | 审查结果保存成功 |
| `parsing` | `blocked` | 附件缺失/下载失败/内容为空/OCR 失败/解析失败 |
| `reviewing` | `blocked` | 规则执行异常/审批接口异常 |
| `pending` | `blocked` | 审批接口异常 |
| `blocked` | `parsing` | 人工重试，`blocked_stage=parsing` |
| `blocked` | `reviewing` | 人工重试，`blocked_stage=reviewing` |

**规范约束：**

- **ST-01-01** 进入 `blocked` **必须**同时写入 `blocked_stage` 与 `error_code`。
- **ST-01-02** 回到 `parsing`/`reviewing` 时**必须**清空 `blocked_stage` 与 `error_code`，`retry_count` 加 1。
- **ST-01-03** **禁止**任何迁移把任务从 `done` 退回 `pending`。
- **ST-01-04** 评论回写失败**禁止**改变 `task_status`（保持 `done`）。
- **ST-01-05** 每次迁移**必须**更新 `updated_at` 并写 `task_logs`。

### ST-02 回写状态

| 从 | 到 | 触发条件 |
|---|---|---|
| — | `not_written` | 任务创建 |
| `not_written` | `writing` | 开始回写 |
| `writing` | `success` | 审批系统返回成功 |
| `writing` | `failed` | 回写失败 |
| `failed` | `writing` | 管理员重新触发 |

- **ST-02-01** `success` 是终态；再次回写**必须**返回既有结果（`duplicate=true`），**禁止**重置为 `writing`。
- **ST-02-02** 每次状态变化**必须**新增一条 `comment_logs`（追加，不覆盖）。

---

## 8. 规则规格

### 8.1 通用规范

- **RL-00-01** 每条规则的判定**必须**先限定 `target_section`（若配置），再在限定范围内匹配；无法定位该部位时回退全文匹配并记 warning。
- **RL-00-02** 命中证据**必须**是 `full_text` 的连续子串，长度 ≤ 300 字符，超出则截断并加 `…`。
- **RL-00-03** 证据位置**必须**由证据文本在 `page_map_json` 中的偏移反查得到，格式遵循 §2.6。
- **RL-00-04** 阈值类规则**禁止**把阈值硬编码在代码里；**必须**来自 `match_params_json` 或 `[CF]` 配置。
- **RL-00-05** 数值比较边界：**严格大于**阈值才命中（等于不命中）。
- **RL-00-06** 所有规则必须可独立测试，输入为 `full_text` + `basic_info` + `clause_info`，输出为 `hit_status` + 证据 + 位置。

### 8.2 RL-001 预付款比例风险

| 项 | 规格 |
|---|---|
| `risk_level` | `high` |
| `match_mode` | `threshold` |
| `target_section` | `payment` |
| 参数 | `{"max_ratio": 0.30}`（缺省回落 `CF-09`） |
| 判定 | 从付款条款中提取预付款比例 `r`；`r > max_ratio` → `hit` |
| 证据 | 包含比例表述的完整句子 |
| 建议 | 建议降低预付款比例或增加履约保障措施（如预付款保函） |
| 边界 | 未提取到比例 → `uncertain`（**禁止**记为 `hit`）；`r = 0.30` → `miss` |
| 正例 | `合同签订后三日内支付合同总额80%` → `hit` |
| 反例 | `签订后支付合同总额的30%` → `miss` |

### 8.3 RL-002 付款周期风险

| 项 | 规格 |
|---|---|
| `risk_level` | `medium` |
| `match_mode` | `threshold` |
| `target_section` | `payment` |
| 参数 | `{"max_days": 90}`（缺省回落 `CF-10`） |
| 判定 | 提取付款账期天数 `d`；`d > max_days` → `hit` |
| 证据 | 含账期表述的完整句子 |
| 建议 | 建议缩短付款周期或增加分期付款节点 |
| 边界 | 无法解析天数 → `uncertain` |
| 正例 | `验收合格后120日内支付尾款` → `hit` |
| 反例 | `验收合格后30日内支付尾款` → `miss` |

### 8.4 RL-003 自动续约风险

| 项 | 规格 |
|---|---|
| `risk_level` | `medium` |
| `match_mode` | `keyword`（主）+ `llm_semantic`（变体兜底） |
| `target_section` | `dispute_clause` 之外的全文 |
| 关键词 | `自动续约`、`自动顺延`、`期满自动`、`默认续期`、`自动延长`、`无异议则续期`、`自动展期` |
| 判定 | 命中任一关键词 → `hit`（`hit_source=rule`）；无关键词则交 LLM 判语义变体（`hit_source=llm`），证据**必须**回验成功 |
| 证据 | 含关键词的完整句子 |
| 建议 | 建议取消默认自动续约，改为到期前书面确认续约 |
| 边界 | 出现"经双方书面确认后可以续约" → `miss`（非默认续约） |

### 8.5 RL-004 违约责任风险

| 项 | 规格 |
|---|---|
| `risk_level` | `high` |
| `match_mode` | `presence` + `llm_semantic` |
| `target_section` | `breach_clause` |
| 判定 | ① 无违约条款（`breach_clause.extract_status != success`）→ `hit`（`hit_source=rule`，证据取最接近条款上下文）；② 有条款 → LLM 判"是否明显不对等"，不对等 → `hit`（`hit_source=llm`） |
| 不对等信号（规则侧辅助） | `单方`、`不承担任何责任`、`全部由乙方承担`、`甲方不承担`、`乙方承担全部` |
| 建议 | 建议调整为双方对等的违约责任条款 |
| 边界 | 双方对等赔偿 → `miss`；LLM 不可用且无不对等信号 → `uncertain` |
| 正例 | `因乙方原因造成甲方损失的，乙方应承担全部赔偿责任；因甲方原因造成乙方损失的，甲方不承担任何责任` → `hit`（**已实测 LLM 判定正确**） |

### 8.6 RL-005 管辖地风险

| 项 | 规格 |
|---|---|
| `risk_level` | `medium` |
| `match_mode` | `regex` |
| `target_section` | `dispute_clause` |
| 参数 | `{"adverse_terms": ["乙方所在地","对方所在地","供应商所在地","卖方所在地","供方所在地"]}` |
| 判定 | 争议解决条款中出现不利管辖地表述 → `hit` |
| 建议 | 建议改为甲方所在地法院管辖或约定明确仲裁机构 |
| 边界 | `甲方所在地`、`我司所在地`、明确仲裁委员会 → `miss` |
| 正例 | `向合同签订地即乙方所在地人民法院提起诉讼` → `hit` |

### 8.7 RL-006 主体信息缺失

| 项 | 规格 |
|---|---|
| `risk_level` | `high` |
| `match_mode` | `presence` |
| 判定 | `party_a` 或 `party_b` 的 `extract_status != success` → `hit` |
| 证据 | 若有部分主体信息，取其原文；全缺则 `evidence_text` 为 `null`、`evidence_position` 为 `null` |
| 建议 | 建议补充完整的签约主体与对方主体信息 |
| 边界 | 两方主体均成功提取 → `miss` |

### 8.8 RL-007 合同金额缺失

| 项 | 规格 |
|---|---|
| `risk_level` | `high` |
| `match_mode` | `presence` |
| 判定 | `contract_amount` 或 `currency` 的 `extract_status != success` → `hit` |
| 建议 | 建议明确合同金额与币种，并保持大小写金额一致 |

### 8.9 RL-008 保密条款缺失

| 项 | 规格 |
|---|---|
| `risk_level` | `medium` |
| `match_mode` | `presence` |
| 判定 | `confidentiality_clause.extract_status != success` → `hit` |
| 建议 | 建议补充保密条款，明确保密范围、期限与违约责任 |

### 8.10 RL-009 数据处理风险

| 项 | 规格 |
|---|---|
| `risk_level` | `medium` |
| `match_mode` | `keyword` + `llm_semantic` |
| 关键词 | `个人信息`、`数据采集`、`数据共享`、`数据处理`、`数据存储`、`数据传输`、`用户数据`、`数据安全` |
| 判定 | ① 命中关键词 → 检查是否同时约定了**目的、范围、安全措施、删除义务**四项；缺任一 → `hit`；② 无关键词但 LLM 判断涉及数据处理 → `hit`（`hit_source=llm`，证据回验） |
| 建议 | 建议补充数据处理的目的、范围、安全措施与数据删除义务 |

### 8.11 RL-010 知识产权风险

| 项 | 规格 |
|---|---|
| `risk_level` | `high` |
| `match_mode` | `presence` + `llm_semantic` |
| 判定 | ① 无知识产权条款 → `hit`（`hit_source=rule`）；② 有条款但 LLM 判定归属不明确 → `hit`（`hit_source=llm`，证据回验） |
| 建议 | 建议明确知识产权归属方、使用许可范围与后续改进成果归属 |
| 边界 | 明确约定"成果归甲方所有" → `miss` |

### 8.12 RL-011 验收标准缺失

| 项 | 规格 |
|---|---|
| `risk_level` | `high` |
| `match_mode` | `presence` + `keyword` |
| 判定 | ① 无验收条款 → `hit`；② 有条款但缺少**验收时间、验收方式、验收标准**三要素中任一 → `hit`，并在 `suggestion` 中指明缺失要素 |
| 建议 | 建议补充验收时间、验收方式及验收标准（模板：`建议补充{缺失要素}`） |
| 正例 | 仅有"甲方验收合格后支付尾款"（无时间、无标准）→ `hit` |

### 8.13 RL-AGG 风险汇总（**规范算法**）

```
令 H = { hit_status = 'hit' 的命中集合 }
若 ∃ h ∈ H 且 h.risk_level = 'high'   → overall = 'high'
否则若 ∃ h ∈ H 且 h.risk_level = 'medium' → overall = 'medium'
否则                                   → overall = 'low'
```

- **RL-AGG-01** `uncertain` 与 `miss` **禁止**参与等级计算。
- **RL-AGG-02** `H` 为空 → `low`（**禁止**因数据缺失判为更高等级）。
- **RL-AGG-03** 整体等级**禁止**由 LLM 决定。

---

## 9. 解析规格

### 9.1 解析流程（**顺序固定**）`[PRD 7.4]`

```
文档类型识别 → 文本提取 → （必要时）OCR → 文本清洗 → 字段提取 → 条款识别
```

| 编号 | 要求 |
|---|---|
| PS-01 | 类型识别**必须**基于文件扩展名 + 文件头魔数双重判定，两者冲突时以魔数为准 |
| PS-02 | 文本清洗**必须**去除连续空白、页眉页脚重复行、孤行连字符；**禁止**改写正文语义 |
| PS-03 | 段落切分**必须**保留页码归属，写入 `page_map_json` |
| PS-04 | 扫描页判定阈值**必须**可配置（`CF-11`）；判定为扫描页的页**必须**逐页 OCR，同一文档可混合 text/ocr 页 |
| PS-05 | 混合文档的 `parse_mode` **必须**取 `ocr`（从严），以保证 position 格式唯一 |
| PS-06 | 字段提取**必须**三层递进：正则/模板 → 条款定位 → LLM 兜底；**禁止**跳过前两层直接用 LLM |
| PS-07 | LLM 兜底仅限关键字段（`contract_amount`、`party_a`、`party_b`、`effective_date`、`expiry_date`） |
| PS-08 | LLM 兜底返回的 `source_text` **必须**经 `full_text` 子串校验；校验失败 → 该字段 `failed` |
| PS-09 | 条款识别**必须**依据条款标题词表（`付款`/`交付`/`验收`/`违约`/`保密`/`数据`/`知识产权`/`争议解决`）及其同义变体 |

### 9.2 证据回验（**强制**）

| 编号 | 要求 |
|---|---|
| PS-10 | 任何写入 `rule_hits.evidence_text` 的文本，**必须**通过 `assert evidence_text in full_text` |
| PS-11 | 回验失败 → 丢弃该证据；若为 LLM 命中则整条降级为 `uncertain`；若为规则命中则**必须**报警为缺陷（规则实现的证据截取有 bug） |

---

## 10. LLM 规格

### 10.1 用途白名单（**仅此三项，其余禁止**）

| 编号 | 允许用途 |
|---|---|
| LM-01 | RL-003 / RL-004 / RL-010 的语义判断 |
| LM-02 | `summary_text` 中文摘要生成 |
| LM-03 | `focus_points` 审批关注点生成 |

> **澄清（M3-F）**：本表的"三项"指**用途**（语义判断 / 摘要 / 关注点），LM-01 后列的规则是**举例而非穷举**。
> §8.10 已明确 RL-009 的 `match_mode = keyword + llm_semantic`，故 **RL-009 亦属 LM-01 的语义判断用途**。
> 采用的解释是"具体条款（§8.x）优先于汇总性列举（本表）"——这是通用解释原则，**不是 §0.2 的规定**。
> 实现共 4 条规则走语义判断（RL-003 / RL-004 / RL-009 / RL-010），单任务最多 4 次调用。
> 详见 `docs/M3-验收记录.md` §5 的 **M3-F**。

**禁止**：用 LLM 决定风险等级（LM-04）、生成证据原文（LM-05）、生成审批结论（LM-06）。

### 10.2 调用规范

| 编号 | 要求 |
|---|---|
| LM-07 | 端点、模型、Key **必须**全部来自 `[CF]` 配置，**禁止**硬编码 |
| LM-08 | `temperature` **必须**为 `0`（判定需可复现） |
| LM-09 | `max_tokens` **必须** ≥ 512（默认 1024）。**推理 token 计入该配额，配额不足会导致 HTTP 200 但 `content` 为空的静默失败** |
| LM-10 | 只解析 `message.content`；`reasoning_content` **禁止**作为回答使用，且**禁止**在多轮上下文中回传 |
| LM-11 | 解析结果**必须**为合法 JSON；**禁止**依赖模型不输出 Markdown 代码块，实现**必须**能剥离 ` ```json ` 包裹 |
| LM-12 | 超时 60 秒；**必须**设置重试上限（≤ 2 次） |
| LM-13 | `content` 为空、非法 JSON、超时、非 200 一律视为 **LLM 调用失败** |

### 10.3 降级（**强制**）

| 编号 | 要求 |
|---|---|
| LM-14 | LLM 失败时**禁止**将任务置为 `blocked` |
| LM-15 | LLM 失败时语义型规则**必须**退化为关键词/条款存在性判定；无法判定时记 `uncertain` |
| LM-16 | LLM 失败时摘要与关注点**必须**退化为模板生成（§10.4） |
| LM-17 | 全部 LLM 调用**必须**记录日志（含耗时、模型、token 数、是否降级），且**禁止**记录 Key |
| LM-18 | `LLM_ENABLED=false` 或 Key 为空时，系统**必须**仍能完成 AC01–AC19 全闭环 |

### 10.4 模板降级文案（**规范文本**）

- `summary_text`：
  `本合同共命中{N}项风险，其中高风险{H}项、中风险{M}项，主要涉及：{规则名顿号连接}。`
  无命中时：`本次自动审查未发现明确的高风险或中风险条款。`
- `focus_points`：取命中项按等级（high > medium）降序，最多 5 条；每条取该规则的 `suggestion_text`，超出 40 字截断加 `…`。
- 无命中时 `focus_points` **必须**为空数组。

---

## 11. 配置规格

| 编号 | 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| CF-01 | `APP_ENV` | 是 | `dev` | 运行环境 |
| CF-02 | `APP_HOST` / `APP_PORT` | 是 | `127.0.0.1` / `8000` | 监听地址 |
| CF-03 | `APP_LOG_LEVEL` | 是 | `INFO` | |
| CF-04 | `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | 是 | — | MySQL 连接 |
| CF-05 | `APPROVAL_BASE_URL` | 是 | `http://127.0.0.1:8100` | 模拟审批系统地址 |
| CF-06 | `APPROVAL_API_KEY` | 是 | — | 工具服务 → 审批系统的鉴权 |
| CF-07 | `INTERNAL_API_KEY` | 是 | — | 调用端 → 工具服务的鉴权（`X-API-Key`） |
| CF-08 | `STORAGE_DIR` | 是 | `./storage/contracts` | 附件存储根目录 |
| CF-09 | `RULE_PREPAY_MAX_RATIO` | 否 | `0.30` | RL-001 默认阈值 |
| CF-10 | `RULE_PAYMENT_MAX_DAYS` | 否 | `90` | RL-002 默认阈值 |
| CF-11 | `OCR_SCAN_PAGE_MIN_CHARS` | 否 | `20` | 扫描页判定阈值（PS-04） |
| CF-12 | `LLM_ENABLED` | 是 | `true` | `false` 则全量降级 |
| CF-13 | `LLM_BASE_URL` | 是 | `https://api.deepseek.com` | |
| CF-14 | `LLM_API_KEY` | 是 | — | **禁止**入库/入日志/提交仓库 |
| CF-15 | `LLM_MODEL` | 是 | `deepseek-flash` | 备选 `deepseek-v4-pro` |
| CF-16 | `LLM_TIMEOUT_SECONDS` | 否 | `60` | |
| CF-17 | `LLM_MAX_TOKENS` | 否 | `1024` | **必须** ≥ 512 |
| CF-18 | `LLM_TEMPERATURE` | 否 | `0` | **必须**为 0 |
| CF-19 | `OCR_LANG` | 否 | `ch` | |
| CF-20 | `OCR_USE_GPU` | 否 | `false` | |
| CF-21 | `OCR_MODEL_DIR` | 否 | `./storage/ocr_models` | 模型缓存（不提交） |

- **CF-22** 所有配置**必须**经 `pydantic-settings` 从环境变量/`.env` 读取，**禁止**在代码中散落 `os.getenv`。
- **CF-23** 启动时**必须**校验必填项，缺失则**必须**启动失败并给出明确错误（fail-fast）。
- **CF-24** `.env` **必须**被 `.gitignore` 忽略；仓库中**仅**保留 `.env.example`。

---

## 12. 非功能规格

| 编号 | 要求 | 依据 |
|---|---|---|
| NF-01 | 任何审查结论**必须**可追溯到：审批任务 → 合同 → 合同原文（`source_text`+`position`）→ 风险规则 | `[PRD 19.1]` |
| NF-02 | 相同审批实例重复拉取**禁止**产生重复任务 | `[PRD 19.2]` |
| NF-03 | 评论回写**必须**幂等，**禁止**重复评论 | `[PRD 19.2]` |
| NF-04 | 任务失败后**禁止**丢失已生成数据；管理员**必须**可从失败阶段重试 | `[PRD 19.3]` |
| NF-05 | 8 类核心操作**必须**留日志 | `[PRD 19.4]` |
| NF-06 | 合同文件**禁止**公开访问；接口**必须**身份校验；日志**必须**脱敏；密钥**禁止**硬编码 | `[PRD 19.5]` |
| NF-07 | 阻塞型操作（PDF 解析、OCR、LLM 调用）**禁止**阻塞事件循环 | 性能 |
| NF-08 | 单任务处理**应当**串行执行，避免并发写同一任务的数据 | 一致性 |
| NF-09 | 接口错误**必须**返回 §5.3 统一结构，**禁止**泄漏堆栈到响应体 | 安全 |
| NF-10 | 前端展示风险等级**必须**有视觉区分（低/中/高三色） | 可用性 |

---

## 13. 验收规格（AC01–AC19 可执行化）

每条给出**验证动作**与**通过判据**。

| AC | 验证动作 | 通过判据 | 关联需求 |
|---|---|---|---|
| AC01 | 调 `IF-01(limit=5)` | 返回 ≥1 条待办，字段齐全 | FR-APP-01 |
| AC02 | 连续两次调 `IF-01` | 第二次 `updated_count=1`、`created_count=0`，`task_id` 不变，库中仅 1 条任务 | FR-APP-02/03 |
| AC03 | 调 `IF-03` | 文件落盘存在，`file_size`>0，`file_checksum` 非空，DT-02 有记录 | FR-ATT-01/02 |
| AC04 | 用 AP-001（文本型 PDF）调 `IF-04` | `parse_status=success`，16 个字段全部产出 FieldRecord | FR-PARSE-01/05 |
| AC05 | 用 AP-003（扫描件）调 `IF-04` | `parse_mode=ocr`，`full_text` 非空且含合同关键要素 | FR-PARSE-02/03 |
| AC06 | 查解析结果 | 8 基本信息 + 8 条款均有值（或明确 `missing`） | FR-PARSE-05 |
| AC07 | 抽查 3 个字段 | `source_text` **是** `full_text` 子串；`position` 符合 §2.6 正则 | FR-PARSE-05、PS-10 |
| AC08 | 调 `IF-05` | 返回 §9 全部 11 条规则的判定结果 | FR-RULE-10 |
| AC09 | 检查命中项 | 每条命中含 `risk_level`+`evidence_text`+`evidence_position`+`suggestion` | FR-RULE-04 |
| AC10 | 检查 AP-001 结果 | `overall_risk_level=high`；手算 RL-AGG 结果一致 | FR-REV-01、RL-AGG |
| AC11 | 检查 `summary_text` | 非空、简体中文、提及主要风险 | FR-REV-02 |
| AC12 | 检查 `focus_points` | 数组、1–5 条、每条 ≤ 40 字、可执行 | FR-REV-03 |
| AC13 | 调 `IF-06` | DT-06 有记录且 `task_id` 唯一；重复调用不新增 | FR-REV-05 |
| AC14 | 检查 `comment_text` | 严格符合 §4.6.1 模板（含标题、等级、摘要、编号关注点、免责声明） | FR-COM-01 |
| AC15 | 调 `IF-07` | DT-07 记 `success` + `remark_id`；审批系统侧出现该评论 | FR-COM-02/03 |
| AC16 | 用 AP-004（附件缺失）跑流程 | 任务 `blocked`、`blocked_stage=parsing`、`error_code=CONTRACT_ATTACHMENT_MISSING` | FR-ATT-04、ST-01 |
| AC17 | 调 `IF-20` 重试 AP-004（补齐附件后） | 任务回到 `parsing` 并最终 `done`，`retry_count=1`，有 `retry` 日志 | FR-LOG-05、ST-01-02 |
| AC18 | 调 `IF-19` | 8 类 `log_type` 均有记录（对应链路至少各 1 条） | FR-LOG-01 |
| AC19 | 从待办拉取到评论回写完整跑一遍 AP-001 | 全链路无人工干预完成，各阶段数据齐全 | 全部 |

---

## 14. 测试矩阵

| TS | 场景 | 覆盖 |
|---|---|---|
| TS-01 | 待办拉取与去重（含并发双拉） | FR-APP-01/02/03 |
| TS-02 | 附件下载成功 / 404 / 网络失败 | FR-ATT-01…05 |
| TS-03 | 文本型 PDF 解析 | FR-PARSE-01/04/05 |
| TS-04 | 扫描件 OCR 解析 | FR-PARSE-02/03 |
| TS-05 | 混合文档（text+ocr 页） | PS-05 |
| TS-06 | 空文档 / 损坏文件 | FR-PARSE-07/08 |
| TS-07 | 16 字段提取完整性 | FR-PARSE-05/10 |
| TS-08 | 证据子串校验（伪造证据必须被拒） | PS-10/11 |
| TS-09 | RL-001…RL-011 每条规则的正例与反例 | §9 全部 |
| TS-10 | RL-AGG 汇总（high/medium/low/空集/全 uncertain） | RL-AGG-01…03 |
| TS-11 | 评论模板渲染（有命中 / 无命中） | §4.6.1 |
| TS-12 | 回写幂等（重复调用） | FR-COM-02/06 |
| TS-13 | 回写失败不删结果、任务保持 done | FR-COM-04、ST-01-04 |
| TS-14 | 状态机非法迁移必须被拒绝 | ST-01-03 |
| TS-15 | 人工重试（parsing 阶段 / reviewing 阶段） | ST-01-02、FR-LOG-05 |
| TS-16 | LLM 降级（关 Key / 超时 / 空 content / 非法 JSON） | LM-13…18 |
| TS-17 | 日志脱敏（不得出现 Key/密码） | NF-06、FR-LOG-03 |
| TS-18 | 内部接口鉴权（缺 `X-API-Key` → 401） | FR-SYS-03 |
| TS-19 | 路径穿越附件名消毒 | FR-ATT-03 |
| TS-20 | 闭环端到端（AC19） | 全部 |

---

## 15. 样例数据规格

**SD-01** 每份样例**必须**随附其期望结果（命中哪些规则、整体等级），用于回归比对。

| 样例 | 类型 | 必须包含的特征 | 期望结果 |
|---|---|---|---|
| AP-001 | 文本型 PDF | 80% 预付款、自动续约条款、无验收标准、对方所在地管辖、双方主体与金额齐全 | 命中 R001/R003/R005/R011 ≥4 条，整体 `high` |
| AP-002 | 文本型 PDF | 条款完备、对等违约、30% 预付款、30 天账期、明确验收、甲方所在地管辖、有保密/数据/知识产权条款 | **0 命中**，整体 `low` |
| AP-003 | 扫描图片 | 关键信息与 AP-001 同类（可简化） | 走 OCR，命中 ≥2 条 |
| AP-004 | 无附件 | 审批单声明有附件但下载 404 | `blocked` / `CONTRACT_ATTACHMENT_MISSING` |
| AP-005 | 空内容或损坏文件 | 0 字节或非法 PDF | `blocked` / `EMPTY_CONTRACT_CONTENT` 或 `PARSE_FAILED` |

**SD-02** 样例合同**必须**为自造文本，**禁止**使用真实企业合同数据。

---

## 16. 未决事项

| 编号 | 事项 | 影响 | 处理 |
|---|---|---|---|
| OPEN-01 | MySQL 异步驱动选型：`asyncmy` 与 `pymysql`（同步）二选一 | 决定 ORM 会话实现风格 | M0 首日实测决定；若 `asyncmy` 无 3.13 轮子则改用同步 Session，并更新本文档 |
| OPEN-02 | 提取字段正则的具体词表 | 影响 AC06/AC07 达成率 | M2 依据样例合同迭代，词表纳入 `docs/` |
| OPEN-03 | RL-009 四项合规要素的判定粒度 | 影响 R009 误报率 | M3 用样例验证后固化 |
| OPEN-04 | 扫描页判定阈值 `OCR_SCAN_PAGE_MIN_CHARS=20` | 影响 text/ocr 分流 | M2 用 AP-003 实测调优 |
| OPEN-05 | 提交文件夹命名 `组名_姓名_项目_行业` 取值 | 交付形式 | 待用户提供 |

---

## 附录 A：与 PRD 的偏离清单（共 5 处，均已确认）

| # | 偏离 | 理由 |
|---|---|---|
| A1 | DT-01 增加 `instance_id`(唯一)、`apply_time`、`attachment_count`、`current_status`、`contract_type`、`form_data_json`、`blocked_stage`、`error_code`、`error_message`、`retry_count` | PRD 第 14 节表结构缺少流程必需字段（去重键、7.1 要求的返回字段、断点重试信息） |
| A2 | DT-04 增加 `match_params_json`、`target_section` | 阈值类规则需要结构化参数；限定匹配部位以降低误报 |
| A3 | DT-02 增加 `attachment_code`、`file_size`、`file_checksum` | 下载接口入参需要审批侧附件编号；PRD 7.3 要求返回大小与校验结果 |
| A4 | DT-03 增加 `attachment_id`、`full_text`、`page_map_json`、`parse_mode`；DT-05 增加 `risk_level`、`suggestion_text`、`hit_source`；DT-07 增加 `remark_id`、`idempotency_key` | 证据回验需全文基准；位置定位需页码映射；命中需快照防漂移；回写需幂等键 |
| A5 | 接口命名统一为 `case_id ≡ approval_tasks.id` | PRD 中 `case_id`/`task_id`/`instance_id` 混用，需明确映射；接口签名保持不变 |

**其余内容严格遵循 PRD**：9 步业务闭环、5 个任务状态、4 个回写状态、7 个工具接口签名、11 条规则、风险汇总算法、字段四元组、评论格式、交付目录结构、AC01–AC19。
