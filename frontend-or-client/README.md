# 调用端（frontend-or-client）

**M6 交付**。Vue 3 + Element Plus + Vite 6 + TypeScript 单页应用，对应 SPEC **FR-UI-01…FR-UI-08** 与 **NF-10**。

## 快速开始

```powershell
cd ClauseGuard/frontend-or-client
npm install
Copy-Item .env.example .env.local      # 填 VITE_INTERNAL_API_KEY
npm run dev                            # http://127.0.0.1:5173
```

前置：工具服务已在 `127.0.0.1:8000` 运行（`backend/README.md` / 项目根 README §6）。

| 命令 | 作用 |
|---|---|
| `npm run dev` | 开发服务器（5173，`strictPort`） |
| `npm run typecheck` | `vue-tsc --noEmit` 类型检查 |
| `npm run build` | 类型检查 + 产出 `dist/` |
| `npm run preview` | 预览构建产物 |

## 页面与 SPEC 对照

| 路由 | 模块 | SPEC |
|---|---|---|
| `/tasks` | 待办调用：审批编号、标题、申请人、申请时间、附件数量、任务状态 | FR-UI-01 |
| `/tasks/{id}/detail` | 详情查看：审批基本信息、表单数据、合同附件 | FR-UI-02 |
| `/tasks/{id}/parse` | 解析结果：合同基本信息与条款 + 原文片段 + 位置 + 提取状态（`missing`/`failed` 视觉区分） | FR-UI-03 |
| `/tasks/{id}/review` | 规则命中：总风险等级（三色）、风险数量、规则名、等级、证据、位置、建议 | FR-UI-04、NF-10 |
| `/tasks/{id}/result` | 结果处理：评论内容、回写状态/时间/错误 + 回写与重试按钮 | FR-UI-05 |
| `/tasks/{id}`（各页页头 + 列表页） | `blocked` 任务人工重试入口 | FR-UI-06 |
| `/tasks/{id}/logs` | 任务日志页（含 8 类核心操作覆盖情况） | FR-UI-07、AC18 |
| `/rules` | 规则维护页：启用/停用、改等级、改建议、新增 | FR-UI-08、IF-21 |

五个业务模块做成 `/tasks/:id/*` 下的**子路由**（真实页面，可直达/刷新/截图），
外壳 `TaskLayout.vue` 负责页头状态与 Tab 切换。

## 约定与设计取舍

1. **走 Vite 代理，不给后端加 CORS**：`/api` → `http://127.0.0.1:8000`。
   浏览器侧同源，鉴权链路（`X-API-Key`）与生产一致。代价是构建产物不能直接双击打开，
   需同源部署或反向代理。
2. **所有请求必须带 `X-API-Key`**（FR-SYS-03）。密钥来源优先级：
   构建期 `VITE_INTERNAL_API_KEY` → 浏览器 localStorage（右上角「接口密钥」对话框）。
   调用端跑在浏览器里，密钥对使用者必然可见——真正的边界是"仅内网可达 + 后端校验"。
3. **合同附件禁止通过静态路径访问**（FR-SYS-02）。详情页只展示附件元数据
   （文件名/类型/大小/SHA-256/下载状态），页面不提供任何直链；查看合同原文请用解析结果页的
   `source_text` 与 `position`。
4. **枚举只在 `src/utils/labels.ts` 映射一次**：视图里不出现 `'high'`/`'blocked'` 这类字面量判断，
   与后端 `app/core/enums.py` 的取值严格对应（禁止扩展、禁止大小写变体）。
5. **NF-10 三色不只靠颜色**：风险等级同时给出色块（绿/橙/红）+ 中文（低/中/高）+ 等级文字。
6. **依赖最小化**：不引入 axios（原生 `fetch`）、pinia（`provide/inject` 足够）、
   unplugin 自动导入（Element Plus 全量引入）、端到端测试框架（原因见 `docs/M6-验收记录.md` M6-C）。

## 目录

```
src/
├── api/            client.ts（请求封装 + ApiError）· types.ts（与后端 schemas 对应）· tasks.ts · rules.ts
├── components/     ApiKeyDialog.vue
├── composables/    taskContext.ts（任务级共享上下文）
├── router/         index.ts
├── utils/          labels.ts（枚举→中文/颜色 的唯一映射 + 时间/大小格式化）
├── views/          TaskListView · TaskLayout · TaskDetailView · ParseResultView
│                   · ReviewHitsView · ResultHandleView · TaskLogsView · RulesView
├── App.vue · main.ts · styles.css · env.d.ts
```

## 类型与接口

`src/api/types.ts` 的每个接口都对应 `backend/app/schemas/*.py` 中的一个模型，
**手写而非生成**：SPEC §5.1/§5.2 的字段名就是契约，手写能一眼看出前端依赖了哪些字段。
后端改了字段名，`npm run typecheck` 会立刻报错。
