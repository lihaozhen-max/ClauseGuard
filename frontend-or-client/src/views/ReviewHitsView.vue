<script setup lang="ts">
/**
 * 规则命中模块（FR-UI-04）。
 *
 * 展示总风险等级、风险数量、规则名、风险等级、证据、位置与建议。
 * 总风险等级按 NF-10 做**低/中/高**三色区分（绿/橙/红），且不只靠颜色——同时给出中文与数值。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { Refresh, VideoPlay } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

import { ApiError, describeError } from '../api/client'
import { getReviewResult, triggerReview } from '../api/tasks'
import type { ReviewPipelineResponse, RuleHit } from '../api/types'
import { useTaskContext } from '../composables/taskContext'
import { useIsNarrow } from '../composables/useViewport'
import {
  formatTime,
  hitSourceLabel,
  hitStatusLabel,
  hitStatusTag,
  riskLevelClass,
  riskLevelLabel,
  riskLevelTag,
} from '../utils/labels'

const { taskId, task, reload: reloadTask } = useTaskContext()
const { narrow } = useIsNarrow()
/** 窄屏下统计块从竖排改成横排，避免与风险徽标挤在一起。 */
const statsColumns = computed(() => (narrow.value ? 3 : 1))

type HitFilter = 'all' | 'hit' | 'risk'

const loading = ref(false)
const acting = ref(false)
const result = ref<ReviewPipelineResponse | null>(null)
const needsParse = ref(false)
const errorMessage = ref('')
const filter = ref<HitFilter>('all')

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ''
  needsParse.value = false
  try {
    result.value = await getReviewResult(taskId.value)
  } catch (error) {
    result.value = null
    if (error instanceof ApiError && error.code === 'PARSE_REQUIRED') {
      needsParse.value = true
    } else {
      errorMessage.value = describeError(error)
    }
  } finally {
    loading.value = false
  }
}

async function onReview(): Promise<void> {
  acting.value = true
  try {
    result.value = await triggerReview(taskId.value)
    needsParse.value = false
    ElMessage.success(
      `审查完成：整体风险${riskLevelLabel(result.value.overall_risk_level)}，命中 ${result.value.hit_count} 条`,
    )
    await reloadTask()
  } catch (error) {
    ElMessage.error(describeError(error))
    await reloadTask()
    await load()
  } finally {
    acting.value = false
  }
}

const hits = computed<RuleHit[]>(() => result.value?.rule_hits ?? [])

const visibleHits = computed<RuleHit[]>(() => {
  if (filter.value === 'hit') return hits.value.filter((item) => item.hit_status === 'hit')
  if (filter.value === 'risk') {
    return hits.value.filter((item) => item.hit_status === 'hit' || item.hit_status === 'uncertain')
  }
  return hits.value
})

function rowClass({ row }: { row: RuleHit }): string {
  return row.hit_status === 'hit' ? 'row-hit' : ''
}

onMounted(load)
watch(taskId, load)
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="card">
      <div class="toolbar">
        <h3 class="card-title" style="margin: 0">规则命中</h3>
        <div class="spacer" />
        <el-button :icon="Refresh" size="small" :loading="loading" @click="load">刷新</el-button>
        <el-button type="primary" size="small" :icon="VideoPlay" :loading="acting" @click="onReview">
          触发审查（IF-16）
        </el-button>
      </div>
    </div>

    <el-alert v-if="errorMessage" type="error" :title="errorMessage" :closable="false" show-icon />

    <div v-if="needsParse" class="card">
      <el-empty description="尚未执行规则审查。若还没解析合同，请先到『解析结果』页触发解析（IF-14）。">
        <el-button type="primary" :loading="acting" @click="onReview">直接触发审查</el-button>
      </el-empty>
    </div>

    <template v-if="result">
      <div class="card">
        <div class="toolbar" style="align-items: center">
          <div :class="['risk-badge', riskLevelClass(result.overall_risk_level)]">
            整体风险：{{ riskLevelLabel(result.overall_risk_level) }}
          </div>
          <el-descriptions :column="statsColumns" size="small" border style="min-width: 220px">
            <el-descriptions-item label="命中风险数">{{ result.hit_count }}</el-descriptions-item>
            <el-descriptions-item label="待人工确认">{{ result.uncertain_count }}</el-descriptions-item>
            <el-descriptions-item label="已评估规则数">{{ result.evaluated_rules }}</el-descriptions-item>
          </el-descriptions>
          <div style="flex: 1; min-width: 280px">
            <el-alert
              v-if="result.summary_degraded"
              type="info"
              :closable="false"
              title="摘要由模板生成（LLM 已降级，LM-18）"
              style="margin-bottom: 8px"
            />
            <div class="pre-block">{{ result.summary_text || '（无摘要）' }}</div>
          </div>
        </div>
      </div>

      <div v-if="result.focus_points.length" class="card">
        <h3 class="card-title">关注点（最多 5 条）</h3>
        <ol style="margin: 0; padding-left: 22px; line-height: 1.9">
          <li v-for="(point, index) in result.focus_points" :key="index">{{ point }}</li>
        </ol>
      </div>

      <div class="card">
        <div class="toolbar" style="margin-bottom: 10px">
          <h3 class="card-title" style="margin: 0">规则判定明细</h3>
          <el-radio-group v-model="filter" size="small">
            <el-radio-button value="all">全部 {{ hits.length }}</el-radio-button>
            <el-radio-button value="hit">仅命中 {{ result.hit_count }}</el-radio-button>
            <el-radio-button value="risk">命中 + 待确认</el-radio-button>
          </el-radio-group>
          <div class="spacer" />
          <span class="muted">
            等级由 RL-AGG 汇总决定，<code>uncertain</code> 与 <code>miss</code> 不参与计算（RL-AGG-01）
          </span>
        </div>

        <el-table :data="visibleHits" border size="small" :row-class-name="rowClass" empty-text="没有符合条件的规则">
          <el-table-column prop="rule_code" label="规则" width="80" />
          <el-table-column prop="rule_name" label="规则名" min-width="170" show-overflow-tooltip />
          <el-table-column label="风险等级" width="100">
            <template #default="{ row }">
              <el-tag :type="riskLevelTag(row.risk_level)" size="small" effect="dark">
                {{ riskLevelLabel(row.risk_level) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="判定" width="100">
            <template #default="{ row }">
              <el-tag :type="hitStatusTag(row.hit_status)" size="small">
                {{ hitStatusLabel(row.hit_status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="来源" width="90">
            <template #default="{ row }">
              <el-tag size="small" effect="plain" type="info">{{ hitSourceLabel(row.hit_source) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="证据（合同原文）" min-width="260">
            <template #default="{ row }">
              <div v-if="row.evidence_text" class="evidence">{{ row.evidence_text }}</div>
              <span v-else class="muted">无原文可定位（缺失型命中）</span>
            </template>
          </el-table-column>
          <el-table-column label="位置" width="150">
            <template #default="{ row }">
              <span class="mono">{{ row.evidence_position || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="建议" min-width="200">
            <template #default="{ row }">
              <div class="evidence">{{ row.suggestion || '—' }}</div>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div v-if="task" class="card">
        <span class="muted">审查结果已入库 · 任务更新时间 {{ formatTime(task.updated_at) }}</span>
      </div>
    </template>
  </div>
</template>
