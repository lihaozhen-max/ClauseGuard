/**
 * 接口数据类型 —— 与后端 `backend/app/schemas/*.py` 一一对应。
 *
 * 这里刻意**手写**而不是代码生成：SPEC §5.1/§5.2 的字段名就是契约，
 * 手写能让人一眼看出前端到底依赖了哪些字段（设计 §11）。
 */

/** SPEC §2.2 枚举（禁止扩展、禁止大小写变体）。 */
export type TaskStatus = 'pending' | 'parsing' | 'reviewing' | 'blocked' | 'done'
export type WriteStatus = 'not_written' | 'writing' | 'success' | 'failed'
export type RiskLevel = 'low' | 'medium' | 'high'
export type RuleStatus = 'enabled' | 'disabled'
export type MatchMode = 'regex' | 'keyword' | 'threshold' | 'presence' | 'llm_semantic'
export type ExtractStatus = 'success' | 'missing' | 'failed'
export type ParseStatus = 'success' | 'partial' | 'failed'
export type ParseMode = 'text' | 'ocr'
export type HitStatus = 'hit' | 'miss' | 'uncertain'
export type HitSource = 'rule' | 'llm'
export type DownloadStatus = 'pending' | 'success' | 'failed'
export type LogLevel = 'info' | 'warning' | 'error'
export type LogType =
  | 'pull'
  | 'download'
  | 'parse'
  | 'ocr'
  | 'extract'
  | 'rule'
  | 'save'
  | 'write_comment'
  | 'retry'

/** SPEC §5.3 统一错误结构。 */
export interface ApiErrorBody {
  error: {
    code: string
    message: string
    task_id: number | null
    detail: Record<string, unknown>
  }
}

export interface PullRequest {
  limit?: number
}

export interface ApprovalListItem {
  instance_id: string
  approval_code: string
  approval_title: string
  applicant_name: string
  apply_time: string | null
  attachment_count: number
  current_status: string | null
  task_id: number
  task_status: TaskStatus
  dedup: 'created' | 'updated'
}

export interface PullResult {
  items: ApprovalListItem[]
  created_count: number
  updated_count: number
}

export interface TaskListItem {
  task_id: number
  instance_id: string
  approval_code: string
  approval_title: string
  applicant_name: string
  apply_time: string | null
  attachment_count: number
  current_status: string | null
  task_status: TaskStatus
  write_status: WriteStatus
}

export interface TaskListResponse {
  items: TaskListItem[]
  total: number
  page: number
  size: number
}

export interface LocalAttachment {
  attachment_id: string
  file_name: string
  file_type: string
  file_size: number | null
  file_checksum: string | null
  download_status: DownloadStatus
}

/** IF-02 的返回：审批系统侧详情（含表单数据，是"表单数据"的权威来源）。 */
export interface ApprovalAttachmentInfo {
  attachment_id: string
  file_name: string
  file_type: string
  file_size: number | null
}

export interface ApprovalDetail {
  instance_id: string
  approval_code: string
  approval_title: string
  applicant_name: string
  apply_time: string | null
  current_status: string | null
  contract_type: string | null
  form_data: Record<string, unknown>
  attachments: ApprovalAttachmentInfo[]
  task_id: number | null
  task_status: TaskStatus | null
}

export interface TaskDetail {
  task_id: number
  instance_id: string
  approval_code: string
  approval_title: string
  applicant_name: string
  apply_time: string | null
  attachment_count: number
  current_status: string | null
  contract_type: string | null
  form_data: Record<string, unknown>
  task_status: TaskStatus
  write_status: WriteStatus
  blocked_stage: string | null
  error_code: string | null
  error_message: string | null
  retry_count: number
  created_at: string
  updated_at: string
  attachments: LocalAttachment[]
}

export interface FieldRecord {
  field_name: string
  field_value: string | null
  source_text: string | null
  position: string | null
  extract_status: ExtractStatus
}

export interface ParseResultResponse {
  document_id: number
  task_id: number
  parse_mode: ParseMode
  parse_status: ParseStatus
  basic_info: FieldRecord[]
  clause_info: FieldRecord[]
  parse_error: string | null
  page_count: number
  used_ocr_pages: number[]
  warnings: string[]
}

export interface RuleHit {
  rule_code: string
  rule_name: string
  risk_level: RiskLevel
  hit_status: HitStatus
  hit_source: HitSource
  evidence_text: string | null
  evidence_position: string | null
  suggestion: string
  reason: string
}

export interface ReviewPipelineResponse {
  case_id: number
  review_id: number | null
  overall_risk_level: RiskLevel
  hit_count: number
  uncertain_count: number
  evaluated_rules: number
  rule_hits: RuleHit[]
  summary_text: string
  focus_points: string[]
  comment_text: string
  summary_degraded: boolean
  task_status: TaskStatus | null
  write_status: WriteStatus | null
  warnings: string[]
}

export interface CommentWriteResult {
  task_id: number
  review_id: number
  write_status: WriteStatus
  remark_id: string | null
  write_response_text: string | null
  duplicate: boolean
}

export interface CommentLog {
  id: number
  task_id: number
  write_status: WriteStatus
  write_response_text: string | null
  remark_id: string | null
  idempotency_key: string
  created_at: string
}

export interface CommentLogList {
  items: CommentLog[]
  total: number
}

export interface TaskLog {
  id: number
  task_id: number
  log_level: LogLevel
  log_type: LogType
  log_content: string
  created_at: string
}

export interface TaskLogList {
  items: TaskLog[]
  total: number
  page: number
  size: number
  /** 该任务出现过的全部 log_type（AC18 直接看这里判断 8 类是否齐备）。 */
  log_types: LogType[]
}

export interface RetryResponse {
  task_id: number
  resumed_stage: 'parsing' | 'reviewing'
  task_status: TaskStatus
  retry_count: number
  review_id: number | null
  overall_risk_level: RiskLevel | null
  comment_text: string
  blocked_stage: string | null
  error_code: string | null
}

/** IF-21 规则维护。 */
export interface RuleModel {
  rule_id: number
  rule_code: string
  rule_name: string
  risk_level: RiskLevel
  rule_status: RuleStatus
  match_mode: MatchMode
  match_text: string | null
  match_params_json: Record<string, unknown> | null
  suggestion_text: string
  target_section: string | null
  updated_at: string
}

export interface RuleListResponse {
  items: RuleModel[]
  total: number
  enabled_count: number
  disabled_count: number
}

export interface RuleUpdateRequest {
  rule_id: number
  rule_name?: string
  risk_level?: RiskLevel
  rule_status?: RuleStatus
  match_mode?: MatchMode
  match_text?: string | null
  match_params_json?: Record<string, unknown> | null
  suggestion_text?: string
  target_section?: string | null
}

export interface RuleCreateRequest {
  rule_code: string
  rule_name: string
  risk_level: RiskLevel
  rule_status?: RuleStatus
  match_mode: MatchMode
  match_text?: string | null
  match_params_json?: Record<string, unknown> | null
  suggestion_text: string
  target_section?: string | null
}
