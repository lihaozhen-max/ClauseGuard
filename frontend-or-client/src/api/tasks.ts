/** 任务相关接口（IF-10…IF-20）。 */

import { request } from './client'
import type {
  CommentLogList,
  CommentWriteResult,
  LogLevel,
  LogType,
  ParseResultResponse,
  PullResult,
  RetryResponse,
  ReviewPipelineResponse,
  TaskDetail,
  TaskListResponse,
  TaskLogList,
  TaskStatus,
} from './types'

/** IF-10 触发待办拉取（按 `instance_id` 去重）。 */
export function pullTasks(limit = 20): Promise<PullResult> {
  return request<PullResult>('/api/tasks/pull', { method: 'POST', body: { limit } })
}

/** IF-11 任务列表（FR-UI-01）。 */
export function listTasks(params: {
  status?: TaskStatus | ''
  page?: number
  size?: number
} = {}): Promise<TaskListResponse> {
  return request<TaskListResponse>('/api/tasks', {
    query: { status: params.status || undefined, page: params.page, size: params.size },
  })
}

/** IF-12 任务详情（FR-UI-02）。 */
export function getTask(taskId: number): Promise<TaskDetail> {
  return request<TaskDetail>(`/api/tasks/${taskId}`)
}

/** IF-13 解析结果（FR-UI-03）；未解析时后端返回 409 `PARSE_REQUIRED`。 */
export function getParseResult(taskId: number): Promise<ParseResultResponse> {
  return request<ParseResultResponse>(`/api/tasks/${taskId}/parse`)
}

/** IF-14 触发解析（下载附件 + 解析 + 字段提取）。 */
export function triggerParse(taskId: number): Promise<ParseResultResponse> {
  return request<ParseResultResponse>(`/api/tasks/${taskId}/parse`, { method: 'POST' })
}

/** IF-15 审查结果（FR-UI-04）。 */
export function getReviewResult(taskId: number): Promise<ReviewPipelineResponse> {
  return request<ReviewPipelineResponse>(`/api/tasks/${taskId}/review`)
}

/** IF-16 触发完整审查并落库。 */
export function triggerReview(taskId: number): Promise<ReviewPipelineResponse> {
  return request<ReviewPipelineResponse>(`/api/tasks/${taskId}/review`, { method: 'POST' })
}

/** IF-17 触发/重试评论回写（幂等）。 */
export function writeComment(taskId: number): Promise<CommentWriteResult> {
  return request<CommentWriteResult>(`/api/tasks/${taskId}/write-comment`, { method: 'POST' })
}

/** IF-18 回写历史。 */
export function getCommentLogs(taskId: number): Promise<CommentLogList> {
  return request<CommentLogList>(`/api/tasks/${taskId}/comment-logs`)
}

/** IF-19 任务日志（FR-UI-07 / AC18）。 */
export function getTaskLogs(
  taskId: number,
  params: { log_type?: LogType | ''; level?: LogLevel | ''; page?: number; size?: number } = {},
): Promise<TaskLogList> {
  return request<TaskLogList>(`/api/tasks/${taskId}/logs`, {
    query: {
      log_type: params.log_type || undefined,
      level: params.level || undefined,
      page: params.page,
      size: params.size,
    },
  })
}

/** IF-20 人工重试 `blocked` 任务（FR-UI-06 / AC17）。 */
export function retryTask(taskId: number): Promise<RetryResponse> {
  return request<RetryResponse>(`/api/tasks/${taskId}/retry`, { method: 'POST' })
}
