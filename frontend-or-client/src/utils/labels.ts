/**
 * 枚举 → 中文标签 / 颜色 的**唯一**映射处（SPEC §2.2 + NF-10）。
 *
 * 视图里禁止再写 `'high'`、`'blocked'` 这类字面量做判断，
 * 否则枚举一变就要满仓库找。NF-10 要求风险等级三色可辨：低=绿、中=橙、高=红。
 */

import type {
  DownloadStatus,
  ExtractStatus,
  HitSource,
  HitStatus,
  LogLevel,
  LogType,
  MatchMode,
  ParseMode,
  ParseStatus,
  RiskLevel,
  RuleStatus,
  TaskStatus,
  WriteStatus,
} from '../api/types'

/** Element Plus 的 `el-tag` / `el-button` 支持的语义色。 */
export type TagType = 'primary' | 'success' | 'info' | 'warning' | 'danger'

function make<T extends string>(map: Record<T, string>) {
  return (value: T | null | undefined): string =>
    value ? (map[value] ?? String(value)) : '—'
}

export const taskStatusLabel = make<TaskStatus>({
  pending: '待处理',
  parsing: '解析中',
  reviewing: '审查中',
  blocked: '已阻塞',
  done: '已完成',
})

export const taskStatusTag = (value: TaskStatus | null | undefined): TagType => {
  switch (value) {
    case 'done':
      return 'success'
    case 'blocked':
      return 'danger'
    case 'reviewing':
      return 'warning'
    case 'parsing':
      return 'primary'
    default:
      return 'info'
  }
}

export const writeStatusLabel = make<WriteStatus>({
  not_written: '未回写',
  writing: '回写中',
  success: '回写成功',
  failed: '回写失败',
})

export const writeStatusTag = (value: WriteStatus | null | undefined): TagType => {
  switch (value) {
    case 'success':
      return 'success'
    case 'failed':
      return 'danger'
    case 'writing':
      return 'warning'
    default:
      return 'info'
  }
}

export const riskLevelLabel = make<RiskLevel>({ low: '低', medium: '中', high: '高' })

/** NF-10：低=绿 / 中=橙 / 高=红，三色必须可辨。 */
export const riskLevelTag = (value: RiskLevel | null | undefined): TagType => {
  switch (value) {
    case 'high':
      return 'danger'
    case 'medium':
      return 'warning'
    case 'low':
      return 'success'
    default:
      return 'info'
  }
}

/** 风险等级对应的 CSS 类（用于整块背景着色，比单纯 tag 更醒目）。 */
export const riskLevelClass = (value: RiskLevel | null | undefined): string =>
  value ? `risk-${value}` : 'risk-unknown'

export const hitStatusLabel = make<HitStatus>({
  hit: '命中',
  miss: '未命中',
  uncertain: '不确定',
})

export const hitStatusTag = (value: HitStatus | null | undefined): TagType => {
  switch (value) {
    case 'hit':
      return 'danger'
    case 'uncertain':
      return 'warning'
    default:
      return 'info'
  }
}

export const hitSourceLabel = make<HitSource>({ rule: '规则', llm: 'LLM 语义' })

export const extractStatusLabel = make<ExtractStatus>({
  success: '已提取',
  missing: '缺失',
  failed: '提取失败',
})

export const extractStatusTag = (value: ExtractStatus | null | undefined): TagType => {
  switch (value) {
    case 'success':
      return 'success'
    case 'missing':
      return 'warning'
    case 'failed':
      return 'danger'
    default:
      return 'info'
  }
}

export const parseStatusLabel = make<ParseStatus>({
  success: '解析成功',
  partial: '部分成功',
  failed: '解析失败',
})

export const parseStatusTag = (value: ParseStatus | null | undefined): TagType => {
  switch (value) {
    case 'success':
      return 'success'
    case 'partial':
      return 'warning'
    default:
      return 'danger'
  }
}

export const parseModeLabel = make<ParseMode>({ text: '文本层', ocr: 'OCR 识别' })

export const ruleStatusLabel = make<RuleStatus>({ enabled: '启用', disabled: '停用' })

export const matchModeLabel = make<MatchMode>({
  regex: '正则',
  keyword: '关键词',
  threshold: '阈值',
  presence: '存在性',
  llm_semantic: 'LLM 语义',
})

export const downloadStatusLabel = make<DownloadStatus>({
  pending: '待下载',
  success: '已下载',
  failed: '下载失败',
})

export const downloadStatusTag = (value: DownloadStatus | null | undefined): TagType => {
  switch (value) {
    case 'success':
      return 'success'
    case 'failed':
      return 'danger'
    default:
      return 'info'
  }
}

export const logLevelLabel = make<LogLevel>({ info: '信息', warning: '警告', error: '错误' })

export const logLevelTag = (value: LogLevel | null | undefined): TagType => {
  switch (value) {
    case 'error':
      return 'danger'
    case 'warning':
      return 'warning'
    default:
      return 'info'
  }
}

/** FR-LOG-01 的 8 类核心操作 + `retry`。 */
export const logTypeLabel = make<LogType>({
  pull: '待办获取',
  download: '附件下载',
  parse: '文档解析',
  ocr: 'OCR 识别',
  extract: '字段提取',
  rule: '规则执行',
  save: '结果保存',
  write_comment: '评论回写',
  retry: '人工重试',
})

/** AC18 要求的 8 类（不含 `retry`）；页面据此提示"是否齐备"。 */
export const CORE_LOG_TYPES: LogType[] = [
  'pull',
  'download',
  'parse',
  'ocr',
  'extract',
  'rule',
  'save',
  'write_comment',
]

export const ALL_LOG_TYPES: LogType[] = [...CORE_LOG_TYPES, 'retry']

export const RISK_LEVELS: RiskLevel[] = ['low', 'medium', 'high']
export const MATCH_MODES: MatchMode[] = ['regex', 'keyword', 'threshold', 'presence', 'llm_semantic']

/** 接口时间（ISO 8601 UTC，形如 `...Z`）→ 本地时间字符串。 */
export function formatTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  )
}

/** 字节数 → 人类可读。 */
export function formatSize(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}
