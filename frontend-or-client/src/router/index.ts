import { createRouter, createWebHistory } from 'vue-router'

import ParseResultView from '../views/ParseResultView.vue'
import ResultHandleView from '../views/ResultHandleView.vue'
import ReviewHitsView from '../views/ReviewHitsView.vue'
import RulesView from '../views/RulesView.vue'
import TaskDetailView from '../views/TaskDetailView.vue'
import TaskLayout from '../views/TaskLayout.vue'
import TaskListView from '../views/TaskListView.vue'
import TaskLogsView from '../views/TaskLogsView.vue'

/**
 * 路由与 SPEC FR-UI-01…FR-UI-08 的对应关系：
 *
 * | 路由 | 模块 | SPEC |
 * |---|---|---|
 * | `/tasks` | 待办调用 | FR-UI-01 |
 * | `/tasks/:id/detail` | 详情查看 | FR-UI-02 |
 * | `/tasks/:id/parse` | 解析结果 | FR-UI-03 |
 * | `/tasks/:id/review` | 规则命中 | FR-UI-04 |
 * | `/tasks/:id/result` | 结果处理（含重试入口） | FR-UI-05、FR-UI-06 |
 * | `/tasks/:id/logs` | 任务日志 | FR-UI-07 |
 * | `/rules` | 规则维护 | FR-UI-08 |
 */
const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/tasks' },
    { path: '/tasks', name: 'tasks', component: TaskListView, meta: { title: '待办任务' } },
    {
      path: '/tasks/:taskId(\\d+)',
      component: TaskLayout,
      children: [
        { path: '', redirect: (to) => ({ name: 'task-detail', params: to.params }) },
        {
          path: 'detail',
          name: 'task-detail',
          component: TaskDetailView,
          meta: { title: '详情查看' },
        },
        { path: 'parse', name: 'task-parse', component: ParseResultView, meta: { title: '解析结果' } },
        { path: 'review', name: 'task-review', component: ReviewHitsView, meta: { title: '规则命中' } },
        {
          path: 'result',
          name: 'task-result',
          component: ResultHandleView,
          meta: { title: '结果处理' },
        },
        { path: 'logs', name: 'task-logs', component: TaskLogsView, meta: { title: '任务日志' } },
      ],
    },
    { path: '/rules', name: 'rules', component: RulesView, meta: { title: '规则维护' } },
    { path: '/:pathMatch(.*)*', redirect: '/tasks' },
  ],
})

router.afterEach((to) => {
  const title = (to.meta.title as string | undefined) ?? ''
  document.title = title ? `${title} — ClauseGuard` : 'ClauseGuard 合同审批审查系统'
})

export default router
