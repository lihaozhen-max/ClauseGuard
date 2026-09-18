<script setup lang="ts">
/**
 * 规则维护页（FR-UI-08 / IF-21）。
 *
 * 三件事：**启用/停用**（列表内开关，立即生效）、**改等级**、**改建议**（编辑对话框）；
 * 另支持新增规则。改完的规则只影响**之后**的审查——历史 `rule_hits` 是命中时的快照
 * （FR-RULE-09），页面顶部明确写出这一点，避免误解为"改了就能改判历史"。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

import { describeError } from '../api/client'
import { createRule, listRules, updateRule } from '../api/rules'
import type { MatchMode, RiskLevel, RuleModel, RuleStatus } from '../api/types'
import {
  MATCH_MODES,
  RISK_LEVELS,
  formatTime,
  matchModeLabel,
  riskLevelLabel,
  riskLevelTag,
  ruleStatusLabel,
} from '../utils/labels'

interface RuleForm {
  rule_id: number | null
  rule_code: string
  rule_name: string
  risk_level: RiskLevel
  match_mode: MatchMode
  match_text: string
  match_params_text: string
  suggestion_text: string
  target_section: string
}

function emptyForm(): RuleForm {
  return {
    rule_id: null,
    rule_code: '',
    rule_name: '',
    risk_level: 'medium',
    match_mode: 'keyword',
    match_text: '',
    match_params_text: '',
    suggestion_text: '',
    target_section: '',
  }
}

const loading = ref(false)
const saving = ref(false)
const rules = ref<RuleModel[]>([])
const enabledCount = ref(0)
const disabledCount = ref(0)
const dialogVisible = ref(false)
const form = reactive<RuleForm>(emptyForm())
const editing = computed(() => form.rule_id !== null)

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await listRules()
    rules.value = data.items
    enabledCount.value = data.enabled_count
    disabledCount.value = data.disabled_count
  } catch (error) {
    ElMessage.error(describeError(error))
  } finally {
    loading.value = false
  }
}

/** 列表内开关：只提交 status，走增量更新（PUT /api/rules）。 */
async function onToggleStatus(rule: RuleModel, next: RuleStatus): Promise<void> {
  try {
    const updated = await updateRule({ rule_id: rule.rule_id, rule_status: next })
    rule.rule_status = updated.rule_status
    enabledCount.value = rules.value.filter((item) => item.rule_status === 'enabled').length
    disabledCount.value = rules.value.length - enabledCount.value
    ElMessage.success(`${rule.rule_code} 已${ruleStatusLabel(updated.rule_status)}，下次审查起生效`)
  } catch (error) {
    ElMessage.error(describeError(error))
    await load()
  }
}

/** 列表内直接改等级（改等级是 FR-UI-08 明确要求的操作）。 */
async function onLevelChange(rule: RuleModel, next: RiskLevel): Promise<void> {
  try {
    const updated = await updateRule({ rule_id: rule.rule_id, risk_level: next })
    rule.risk_level = updated.risk_level
    ElMessage.success(`${rule.rule_code} 风险等级改为「${riskLevelLabel(updated.risk_level)}」`)
  } catch (error) {
    ElMessage.error(describeError(error))
    await load()
  }
}

function openCreate(): void {
  Object.assign(form, emptyForm())
  dialogVisible.value = true
}

function openEdit(rule: RuleModel): void {
  Object.assign(form, {
    rule_id: rule.rule_id,
    rule_code: rule.rule_code,
    rule_name: rule.rule_name,
    risk_level: rule.risk_level,
    match_mode: rule.match_mode,
    match_text: rule.match_text ?? '',
    match_params_text: rule.match_params_json ? JSON.stringify(rule.match_params_json) : '',
    suggestion_text: rule.suggestion_text,
    target_section: rule.target_section ?? '',
  })
  dialogVisible.value = true
}

function parseParams(): Record<string, unknown> | null {
  const text = form.match_params_text.trim()
  if (!text) return null
  const parsed: unknown = JSON.parse(text)
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new Error('match_params_json 必须是 JSON 对象，例如 {"max_ratio": 0.3}')
  }
  return parsed as Record<string, unknown>
}

async function onSave(): Promise<void> {
  let params: Record<string, unknown> | null
  try {
    params = parseParams()
  } catch (error) {
    ElMessage.error((error as Error).message)
    return
  }

  saving.value = true
  try {
    if (editing.value && form.rule_id !== null) {
      await updateRule({
        rule_id: form.rule_id,
        rule_name: form.rule_name,
        risk_level: form.risk_level,
        match_mode: form.match_mode,
        match_text: form.match_text || null,
        match_params_json: params,
        suggestion_text: form.suggestion_text,
        target_section: form.target_section || null,
      })
      ElMessage.success(`${form.rule_code} 已保存`)
    } else {
      await createRule({
        rule_code: form.rule_code,
        rule_name: form.rule_name,
        risk_level: form.risk_level,
        match_mode: form.match_mode,
        match_text: form.match_text || null,
        match_params_json: params,
        suggestion_text: form.suggestion_text,
        target_section: form.target_section || null,
      })
      ElMessage.success(`规则 ${form.rule_code} 已创建（默认启用）`)
    }
    dialogVisible.value = false
    await load()
  } catch (error) {
    ElMessage.error(describeError(error))
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="card">
      <div class="toolbar">
        <h3 class="card-title" style="margin: 0">规则维护</h3>
        <el-tag size="small" type="info">共 {{ rules.length }} 条</el-tag>
        <el-tag size="small" type="success">启用 {{ enabledCount }}</el-tag>
        <el-tag size="small" type="warning">停用 {{ disabledCount }}</el-tag>
        <div class="spacer" />
        <el-button :icon="Refresh" size="small" :loading="loading" @click="load">刷新</el-button>
        <el-button type="primary" size="small" :icon="Plus" @click="openCreate">新增规则</el-button>
      </div>
      <p class="muted" style="margin: 12px 0 0">
        只有 <code>rule_status=enabled</code> 的规则会参与审查（FR-RULE-01）。修改只影响**之后**的审查：
        已入库的命中是当时的快照（FR-RULE-09），不会被回改。
      </p>
    </div>

    <div class="card">
      <el-table :data="rules" border size="small" empty-text="暂无规则，请先执行 database/seed_rules.sql">
        <el-table-column prop="rule_code" label="编码" width="80" />
        <el-table-column prop="rule_name" label="规则名" min-width="190" show-overflow-tooltip />
        <el-table-column label="风险等级" width="130">
          <template #default="{ row }">
            <el-select
              :model-value="row.risk_level"
              size="small"
              @change="(value: RiskLevel) => onLevelChange(row, value)"
            >
              <el-option v-for="item in RISK_LEVELS" :key="item" :label="riskLevelLabel(item)" :value="item" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="等级预览" width="90">
          <template #default="{ row }">
            <el-tag :type="riskLevelTag(row.risk_level)" size="small" effect="dark">
              {{ riskLevelLabel(row.risk_level) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="匹配模式" width="110">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ matchModeLabel(row.match_mode) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="匹配内容" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="mono">{{ row.match_text || (row.match_params_json ? JSON.stringify(row.match_params_json) : '—') }}</span>
          </template>
        </el-table-column>
        <el-table-column label="建议" min-width="220">
          <template #default="{ row }">
            <div class="evidence">{{ row.suggestion_text }}</div>
          </template>
        </el-table-column>
        <el-table-column label="适用范围" width="110" prop="target_section" />
        <el-table-column label="启用" width="80">
          <template #default="{ row }">
            <el-switch
              :model-value="row.rule_status === 'enabled'"
              size="small"
              @change="(value: boolean) => onToggleStatus(row, value ? 'enabled' : 'disabled')"
            />
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="160">
          <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="80" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog
      v-model="dialogVisible"
      :title="editing ? `编辑规则 ${form.rule_code}` : '新增规则'"
      width="640px"
    >
      <el-form label-width="120px" size="small">
        <el-form-item label="规则编码" required>
          <el-input
            v-model="form.rule_code"
            :disabled="editing"
            placeholder="形如 R012"
            maxlength="16"
          />
          <span v-if="editing" class="muted">规则编码是规则的对外标识，创建后不可修改</span>
        </el-form-item>
        <el-form-item label="规则名" required>
          <el-input v-model="form.rule_name" maxlength="128" />
        </el-form-item>
        <el-form-item label="风险等级" required>
          <el-radio-group v-model="form.risk_level">
            <el-radio-button v-for="item in RISK_LEVELS" :key="item" :value="item">
              {{ riskLevelLabel(item) }}
            </el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="匹配模式" required>
          <el-select v-model="form.match_mode" style="width: 200px">
            <el-option v-for="item in MATCH_MODES" :key="item" :label="matchModeLabel(item)" :value="item" />
          </el-select>
        </el-form-item>
        <el-form-item label="匹配内容">
          <el-input v-model="form.match_text" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="匹配参数">
          <el-input
            v-model="form.match_params_text"
            type="textarea"
            :rows="2"
            placeholder='阈值类/语义类规则用，例如 {"max_ratio": 0.3}'
          />
        </el-form-item>
        <el-form-item label="建议" required>
          <el-input v-model="form.suggestion_text" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="适用范围">
          <el-input v-model="form.target_section" placeholder="basic_info / clause_info，可留空" maxlength="32" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>
