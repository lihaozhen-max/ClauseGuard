# 额外测试合同（T-01…T-04）

这是**给你自己动手测**的合同，与随系统交付的固定样例（`../AP-001…AP-005`）分开放。
四份都是**虚构主体**（"某某…"），符合 SPEC §15 SD-02「禁止真实企业合同数据」。

## 它们各自打在哪

| 文件 | 形态 | 设计意图 | **实测结果** |
|---|---|---|---|
| `T-01_设备租赁合同.pdf` | 文本型 PDF | 故意写"差"：预付款 50%、尾款 120 天、自动续约、向乙方所在地起诉、无保密、无知识产权、验收无标准 | **high，命中 8 条**：R001 R002 R003 R004 R005 R008 R010 R011 |
| `T-02_技术服务合同.pdf` | 文本型 PDF | 条款齐备的"好"合同 | **low，0 命中**（对照组） |
| `T-03_数据处理服务协议.docx` | Word | 测 docx 解析 + 打 R009（涉及个人信息，但缺"删除义务"） | **medium，命中 1 条**：R009 |
| `T-04_框架采购协议.pdf` | 文本型 PDF | 主体名称与金额都空着 | **high**：命中 R006 R007；**R001 待确认**（没有金额又不约定预付款，算不出比例 → 交人工） |

覆盖到的规则路径：阈值（R001 R002）、关键词（R003 R009）、正则（R005）、存在性（R006 R007 R008 R010 R011）、
以及 LLM 语义（R004 由 LLM 判定"违约责任是否对等"）、`uncertain` 分支（R001）、缺失字段（T-04 的解析页会出现 missing 行）。

> T-02 与 T-03 还各自兼任**回归样本**：T-02 用「组织验收」、T-03 用合并标题「交付与验收」，
> 这两种写法原先都会被误判（详见文末「这两份样例同时是回归样本」）。

## 怎么测

### 一条命令（推荐）

```powershell
cd ClauseGuard
uv run --project backend python scripts/try_contract.py sample_contracts/extra/T-01_设备租赁合同.pdf
```

它会：借一个审批单的槽位（默认 AP-004，它的附件本来就缺，最无损）→ 备份原文件 →
重置该任务的附件/解析/审查痕迹 → 跑 `解析 → 审查` → 打印整体风险、逐条规则判定、证据原文与位置 →
**自动还原槽位**。测完打开它给的链接就能在界面上看完整结果。

```powershell
# 换槽位（AP-001…AP-005 都行）
uv run --project backend python scripts/try_contract.py <你的合同.pdf> --instance AP-002

# 连续多次解析同一份合同（看判定是否稳定，尤其 LLM 相关规则）
uv run --project backend python scripts/try_contract.py <你的合同.pdf> --keep-slot
```

**支持的格式**：PDF（文本型）、Word（`.docx`）、扫描件（`.png` / `.jpg`，会走 OCR，单页约 60–150 秒）。

### 不用脚本也行

```powershell
# 1) 把你的合同复制成 AP-004 声明的文件名（它的附件本来是故意缺失的）
Copy-Item 你的合同.pdf sample_contracts\AP-004_办公用品采购合同.pdf
# 2) 到界面上点 AP-004 的「触发解析」，再看「规则命中」
# 3) 测完删掉，恢复 AC16 基线
Remove-Item sample_contracts\AP-004_办公用品采购合同.pdf
```

## 怎么改成你自己的合同

文本源在 `sources/*.txt`，改完重跑生成器即可（也会顺带更新 PDF/Word 产物）：

```powershell
uv run --project backend python sample_contracts/extra/generate_extra.py
# 想把某份做成扫描件来测 OCR：
uv run --project backend python sample_contracts/extra/generate_extra.py --scan T-02
```

写文本时留意抽取器认这些措辞（不然字段会全是 missing，测了也看不出东西）：

| 字段 | 写法 |
|---|---|
| 标题 / 编号 | `合同标题：…` / `合同编号：CG-XXX-2026-0001` |
| 主体 | `甲方（承租方）：某某…公司` / `乙方（出租方）：某某…公司` |
| 金额 / 币种 | `人民币叁拾陆万元整（¥360000.00）` / `币种为人民币（CNY）` |
| 日期 | `自2026年11月10日起生效，有效期至2027年11月9日止` |
| 条款 | 用 `第X条  名称`，名称里要含关键词（付款/交付/验收/违约/保密/知识产权/争议解决） |

## 这两份样例同时是回归样本（原先测出的两处问题**已修复**）

写这几份合同时暴露了两个真实问题。它们不是"样例写错了"，而是规则数据与解析器的口径问题；
**两处都已修**，而 T-02 / T-03 现在就保留着当初触发问题的写法，当作**活体回归样本**。

**① R011 的「验收方式」词表过窄 → 已修。**
原词表只认 `方式 / 流程 / 程序 / 书面 / 报告 / 检测 / 抽检 / 进行验收`，
于是"甲方应在交付后10个工作日内**组织验收**"被判成"缺验收方式"，
让一份验收条款写得很清楚的合同（T-02）被误报一条高风险。
现在词表扩到 **13 项**，加入 `组织验收`、`验收合格`、`验收通过`、`核查`、`由…验收`
（规则数据在 `database/seed_rules.sql` 的 R011 行）。

- 证据：T-02 现在用的就是「**组织验收**」，实测 **low / 0 命中**；
- 回归用例：`tests/test_m3_predicates.py::test_r011_miss_when_acceptance_is_organized`。

**② 合并标题「交付与验收」丢掉验收字段 → 已修。**
解析器原来一个条款标题只归**一个**字段（按词表顺序先命中者得）：
"交付"抢走标题后 `acceptance_clause` 拿不到区间 → 该字段提取状态变 `missing`
→ R011 直接判"合同未约定验收条款"。
现在**一个标题可以同时归多个字段**（`backend/app/modules/parser/fields.py` 的
`clause_heading_index` 返回元组）。

- 证据：T-03 的标题就是「**第五条  交付与验收**」，实测 `未成功提取的字段：无`，
  结果是 **medium / 命中 1（仅 R009）**；
- 回归用例：`tests/test_m2_parse.py::test_merged_heading_maps_to_multiple_clauses`。

> ⚠️ **规则词表存在数据库里**。如果你之前已经跑过旧版本，需要重新灌一次种子才会生效：
>
> ```powershell
> cmd /c "type database\seed_rules.sql | docker exec -i clauseguard-mysql mysql -uroot -p<DB_ROOT_PASSWORD> --default-character-set=utf8mb4 clauseguard"
> ```
>
> 别用 `Get-Content ... -Raw | ...`：Windows PowerShell 5.1 会按 ANSI 解码**无 BOM** 的 UTF-8 文件，
> 中文会变成乱码再灌进库。
