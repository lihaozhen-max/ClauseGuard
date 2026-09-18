<script setup lang="ts">
/**
 * 解析结果模块（FR-UI-03）。
 *
 * 展示合同基本信息与条款（各 8 条，SPEC §2.5），每条含字段值、原文片段、位置与提取状态。
 * `missing` / `failed` **必须视觉区分**：整行底色 + 状态标签双重提示，不只靠颜色（可读性）。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { Refresh, VideoPlay } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

import { ApiError, describeError } from '../api/client'
import { getParseResult, triggerParse } from '../api/tasks'
import type { FieldRecord, ParseResultResponse } from '../api/types'
import { useTaskContext } from '../composables/taskContext'
import {
  extractStatusLabel,
  extractStatusTag,
  parseModeLabel,
  parseStatusLabel,
  parseStatusTag,
} from '../utils/labels'

const { taskId, reload: reloadTask } = useTaskContext()

const loading = ref(false)
const acting = ref(false)
const result = ref<ParseResultResponse | null>(null)
const notParsed = ref(false)
const errorMessage = ref('')

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ''
  notParsed.value = false
  try {
    result.value = await getParseResult(taskId.value)
  } catch (error) {
    result.value = null
    // 未解析是"正常的前置条件"，不是错误页 —— 给一个可以直接点的动作
    if (error instanceof ApiError && error.code === 'PARSE_REQUIRED') {
      notParsed.value = true
    } else {
      errorMessage.value = describeError(error)
    }
  } finally {
    loading.value = false
  }
}

async function onParse(): Promise<void> {
  acting.value = true
  try {
    result.value = await triggerParse(taskId.value)
    notParsed.value = false
    ElMessage.success(`解析完成：${parseStatusLabel(result.value.parse_status)}`)
    await reloadTask()
  } catch (error) {
    ElMessage.error(describeError(error))
    await reloadTask()
  } finally {
    acting.value = false
  }
}

/** `missing` / `failed` 行加底色（CSS 在 styles.css，与标签颜色语义一致）。 */
function rowClass({ row }: { row: FieldRecord }): string {
  return row.extract_status === 'success' ? '' : `row-extract-${row.extract_status}`
}

onMounted(load)
watch(taskId, load)

const warnings = computed(() => result.value?.warnings ?? [])
const usedOcrPages = computed(() => result.value?.used_ocr_pages ?? [])
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="card">
      <div class="toolbar">
        <h3 class="card-title" style="margin: 0">解析结果</h3>
        <template v-if="result">
          <el-tag :type="parseStatusTag(result.parse_status)" size="small">
            {{ parseStatusLabel(result.parse_status) }}
          </el-tag>
          <el-tag size="small" effect="plain">{{ parseModeLabel(result.parse_mode) }}</el-tag>
          <el-tag size="small" effect="plain" type="info">共 {{ result.page_count }} 页</el-tag>
          <el-tag v-if="usedOcrPages.length" size="small" effect="plain" type="warning">
            OCR 页：{{ usedOcrPages.join('、') }}
          </el-tag>
        </template>
        <div class="spacer" />
        <el-button :icon="Refresh" size="small" :loading="loading" @click="load">刷新</el-button>
        <el-button type="primary" size="small" :icon="VideoPlay" :loading="acting" @click="onParse">
          触发解析（IF-14）
        </el-button>
      </div>
    </div>

    <el-alert v-if="errorMessage" type="error" :title="errorMessage" :closable="false" show-icon />

    <div v-if="notParsed" class="card">
      <el-empty description="该任务尚未解析">
        <el-button type="primary" :loading="acting" @click="onParse">立即解析</el-button>
      </el-empty>
    </div>

    <template v-if="result">
      <el-alert
        v-if="result.parse_error"
        type="error"
        :title="`解析失败：${result.parse_error}`"
        :closable="false"
        show-icon
      />
      <el-alert
        v-for="(warning, index) in warnings"
        :key="index"
        type="warning"
        :title="warning"
        :closable="false"
        show-icon
      />

      <div class="card">
        <h3 class="card-title">
          合同基本信息
          <span class="muted">（{{ result.basic_info.length }} 项，SPEC §2.5）</span>
        </h3>
        <el-table :data="result.basic_info" border size="small" :row-class-name="rowClass">
          <el-table-column prop="field_name" label="字段" width="130" />
          <el-table-column label="提取值" min-width="180">
            <template #default="{ row }">
              <span :class="{ muted: !row.field_value }">{{ row.field_value || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="原文片段" min-width="260">
            <template #default="{ row }">
              <div class="evidence">{{ row.source_text || '—' }}</div>
            </template>
          </el-table-column>
          <el-table-column prop="position" label="位置" width="150">
            <template #default="{ row }">
              <span class="mono">{{ row.position || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="提取状态" width="110">
            <template #default="{ row }">
              <el-tag :type="extractStatusTag(row.extract_status)" size="small">
                {{ extractStatusLabel(row.extract_status) }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div class="card">
        <h3 class="card-title">
          条款信息
          <span class="muted">（{{ result.clause_info.length }} 项，SPEC §2.5）</span>
        </h3>
        <el-table :data="result.clause_info" border size="small" :row-class-name="rowClass">
          <el-table-column prop="field_name" label="条款" width="130" />
          <el-table-column label="条款内容" min-width="200">
            <template #default="{ row }">
              <span :class="{ muted: !row.field_value }">{{ row.field_value || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="原文片段" min-width="260">
            <template #default="{ row }">
              <div class="evidence">{{ row.source_text || '—' }}</div>
            </template>
          </el-table-column>
          <el-table-column prop="position" label="位置" width="150">
            <template #default="{ row }">
              <span class="mono">{{ row.position || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="提取状态" width="110">
            <template #default="{ row }">
              <el-tag :type="extractStatusTag(row.extract_status)" size="small">
                {{ extractStatusLabel(row.extract_status) }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
        <p class="muted" style="margin-bottom: 0">
          底色说明：<b>缺失</b>为浅橙、<b>提取失败</b>为浅红（FR-UI-03 要求两者视觉区分）。
        </p>
      </div>
    </template>
  </div>
</template>
