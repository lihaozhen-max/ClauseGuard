/**
 * 任务级上下文：由 `TaskLayout` 载入一次任务详情，供五个模块共享。
 *
 * 这样"详情/解析/规则命中/结果处理/日志"五个页面共用同一个任务对象与刷新入口，
 * 在任一模块里触发的动作（解析、审查、重试）都能让页头状态立刻同步。
 */

import type { ComputedRef, InjectionKey, Ref } from 'vue'
import { inject, provide } from 'vue'

import type { ApprovalDetail, TaskDetail } from '../api/types'

export interface TaskContext {
  /** 当前任务 id（来自路由参数）。 */
  taskId: ComputedRef<number>
  task: Ref<TaskDetail | null>
  /** IF-02 审批系统侧详情（表单数据的权威来源）；不可达时为 `null`。 */
  approval: Ref<ApprovalDetail | null>
  /** 审批系统不可达时的原因（用于页面提示，不当作致命错误）。 */
  approvalError: Ref<string>
  loading: Ref<boolean>
  error: Ref<string>
  reload: () => Promise<void>
}

const TASK_CONTEXT_KEY: InjectionKey<TaskContext> = Symbol('clauseguard.task-context')

export function provideTaskContext(context: TaskContext): void {
  provide(TASK_CONTEXT_KEY, context)
}

export function useTaskContext(): TaskContext {
  const context = inject(TASK_CONTEXT_KEY)
  if (!context) {
    throw new Error('TaskContext 未提供：本组件必须挂在 /tasks/:taskId 的子路由下')
  }
  return context
}
