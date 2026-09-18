/** 规则维护接口（IF-21 / FR-UI-08）。 */

import { request } from './client'
import type { RuleCreateRequest, RuleListResponse, RuleModel, RuleUpdateRequest } from './types'

/** IF-21 规则查询（含停用规则）。 */
export function listRules(): Promise<RuleListResponse> {
  return request<RuleListResponse>('/api/rules')
}

/** IF-21 规则新增。 */
export function createRule(payload: RuleCreateRequest): Promise<RuleModel> {
  return request<RuleModel>('/api/rules', { method: 'POST', body: payload })
}

/** IF-21 规则修改（`rule_id` 置于请求体，见接口说明）。 */
export function updateRule(payload: RuleUpdateRequest): Promise<RuleModel> {
  return request<RuleModel>('/api/rules', { method: 'PUT', body: payload })
}
