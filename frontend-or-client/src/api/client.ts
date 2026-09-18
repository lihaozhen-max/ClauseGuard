/**
 * 内部 REST 的统一请求封装（FR-SYS-03）。
 *
 * 三件事收敛在这里：带 `X-API-Key`、解包 SPEC §5.3 统一错误结构、把失败抛成 `ApiError`。
 * 视图层只处理 `ApiError`，不必各自判断 HTTP 状态码。
 */

import type { ApiErrorBody } from './types'

const API_KEY_STORAGE = 'clauseguard.internal_api_key'

/** 构建期默认值（`VITE_INTERNAL_API_KEY`），没配就走页面上的对话框。 */
const BUILD_TIME_KEY = import.meta.env.VITE_INTERNAL_API_KEY ?? ''

export function getApiKey(): string {
  return localStorage.getItem(API_KEY_STORAGE) || BUILD_TIME_KEY
}

export function hasApiKey(): boolean {
  return getApiKey().length > 0
}

export function setApiKey(value: string): void {
  const trimmed = value.trim()
  if (trimmed) {
    localStorage.setItem(API_KEY_STORAGE, trimmed)
  } else {
    localStorage.removeItem(API_KEY_STORAGE)
  }
}

/** 后端返回的统一错误（SPEC §5.3）。 */
export class ApiError extends Error {
  readonly code: string
  readonly status: number
  readonly taskId: number | null
  readonly detail: Record<string, unknown>

  constructor(
    status: number,
    code: string,
    message: string,
    taskId: number | null = null,
    detail: Record<string, unknown> = {},
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.taskId = taskId
    this.detail = detail
  }

  /** 任务被阻塞 / 前置条件不满足时，页面通常想给出不同的下一步提示。 */
  get isBlocked(): boolean {
    return this.code === 'CONTRACT_ATTACHMENT_MISSING'
      || this.code === 'DOWNLOAD_FAILED'
      || this.code === 'EMPTY_CONTRACT_CONTENT'
      || this.code === 'OCR_FAILED'
      || this.code === 'PARSE_FAILED'
      || this.code === 'RULE_EXECUTION_FAILED'
      || this.code === 'APPROVAL_API_ERROR'
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT'
  body?: unknown
  query?: Record<string, string | number | undefined | null>
  signal?: AbortSignal
}

function buildUrl(path: string, query: RequestOptions['query']): string {
  if (!query) return path
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue
    params.append(key, String(value))
  }
  const qs = params.toString()
  return qs ? `${path}?${qs}` : path
}

async function parseError(response: Response): Promise<ApiError> {
  let payload: ApiErrorBody | null = null
  try {
    payload = (await response.json()) as ApiErrorBody
  } catch {
    payload = null
  }
  const error = payload?.error
  if (error) {
    return new ApiError(response.status, error.code, error.message, error.task_id, error.detail ?? {})
  }
  // 非统一结构（例如代理层 502）也要给出可读信息，不能吞掉
  return new ApiError(response.status, 'NETWORK_ERROR', `请求失败（HTTP ${response.status}）`)
}

/**
 * 发请求并返回 JSON。
 *
 * @throws {ApiError} 非 2xx，或响应体不是合法 JSON
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  const key = getApiKey()
  if (key) headers['X-API-Key'] = key
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'

  let response: Response
  try {
    response = await fetch(buildUrl(path, options.query), {
      method: options.method ?? 'GET',
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    })
  } catch (cause) {
    throw new ApiError(0, 'NETWORK_ERROR', `无法连接工具服务：${(cause as Error).message}`)
  }

  if (!response.ok) throw await parseError(response)
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

/** 把任意异常转成一句可直接展示的中文提示。 */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    const parts = [`[${error.code}] ${error.message}`]
    if (error.taskId !== null) parts.push(`task_id=${error.taskId}`)
    return parts.join(' | ')
  }
  if (error instanceof Error) return error.message
  return String(error)
}
