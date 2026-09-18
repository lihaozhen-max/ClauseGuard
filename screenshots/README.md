# 测试截图（screenshots）

按实战说明要求，提交文件夹必须附测试截图。本目录**必须提交**（`.gitignore` 中已显式排除忽略规则）。

## 命名约定

```
AC01_待办拉取.png
AC02_重复拉取去重.png
AC04_AP001解析结果.png
AC05_AP003_OCR解析.png
AC09_规则命中证据.png
AC14_评论模板.png
AC16_AP004_blocked.png
AC17_人工重试.png
AC18_任务日志.png
AC19_端到端闭环.png
```

## 已入库（M6，调用端真实渲染）

| 文件 | 对应 | 内容 |
|---|---|---|
| `AC01_待办拉取.png` | AC01 / FR-UI-01 | 待办列表：审批编号、标题、申请人、申请时间、附件数、任务状态、回写状态 |
| `AC04_AP001解析结果.png` | AC04 / FR-UI-03 | AP-001 的 8 项基本信息 + 8 项条款，含原文片段、位置、提取状态 |
| `AC09_规则命中证据.png` | AC09 / FR-UI-04 | 整体风险「高」（红色块）、命中 6 条、R001–R011 明细含证据与建议 |
| `AC14_评论模板.png` | AC14 / FR-UI-05 | §4.6.1 模板评论正文 + 回写状态/时间/备注号 + 回写历史 |
| `AC16_AP004_blocked.png` | AC16 / FR-UI-06 | AP-004 阻塞于 `parsing`：`CONTRACT_ATTACHMENT_MISSING` + 重试按钮 |
| `AC18_任务日志.png` | AC18 / FR-UI-07 | 8 类核心操作覆盖情况 + 全链路日志明细 |
| `FR-UI-08_规则维护.png` | FR-UI-08 / IF-21 | 规则列表：启停开关、等级下拉、建议、新增/编辑 |

## 待补（M7）

- `AC05_AP003_OCR解析.png`：扫描件（AP-003）走 OCR 的解析结果（单页约 60s，需先触发解析）；
- `AC17_人工重试.png`：点「人工重试」后任务由 `blocked` 转为 `done` 的界面状态；
- `AC19_端到端闭环.png`：一次完整九步闭环的最终任务状态与回写结果；
- 交互态截图（如「新增规则」对话框、解析结果的 `missing` 行高亮）——headless 截图不含交互态，需人工补拍。

## 生成方式（当前批次）

由 headless Chrome 对**真实运行的前后端**渲染后截图，非手绘、非拼接：

```powershell
& chrome --headless=new --disable-gpu --hide-scrollbars --window-size=1760,1080 `
   --virtual-time-budget=9000 --screenshot="screenshots\AC01_待办拉取.png" `
   "http://127.0.0.1:5173/tasks"
```

## 约束

- 每张截图对应 SPEC §13 的一条验收标准（AC01–AC19）或一条 FR-UI，便于逐条核对；
- 截图不得包含 `.env` 内容、API Key、数据库口令等敏感信息（NF-06）；
  已入库的 7 张均不含密钥——密钥输入框仅在点击「接口密钥」后出现，截图时未打开。
