<script setup lang="ts">
/**
 * 任务日志页（FR-UI-07 / AC18）。
 *
 * 后端 IF-19 除了分页结果，还会回 `log_types`（该任务出现过的全部类型），
 * 页面据此直接给出"8 类核心操作是否齐备"的判断——这正是 AC18 要看的证据。
 *
 * 注意：**文本型合同没有 OCR 环节**，因此只有 7 类是预期语义，不是缺陷。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

import { describeError } from '../api/client'
import { getTaskLogs } from '../api/tasks'
import type { LogLevel, LogType, TaskLog } from '../api/types'
import { useTaskContext } from '../composables/taskContext'
import {
  ALL_LOG_TYPES,
  CORE_LOG_TYPES,
  formatTime,
  logLevelLabel,
  logLevelTag,
  logTypeLabel,
} from '../utils/labels'

const { taskId } = useTaskContext()

const loading = ref(false)
const rows = ref<TaskLog[]>([])
const total = ref(0)
const presentTypes = ref<LogType[]>([])
const page = ref(1)
const size = ref(50)
const filterType = ref<LogType | ''>('')
const filterLevel = ref<LogLevel | ''>('')

const LEVELS: LogLevel[] = ['info', 'warning', 'error']

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await getTaskLogs(taskId.value, {
      log_type: filterType.value,
      level: filterLevel.value,
      page: page.value,
      size: size.value,
    })
    rows.value = data.items
    total.value = data.total
    presentTypes.value = data.log_types
  } catch (error) {
    ElMessage.error(describeError(error))
  } finally {
    loading.value = false
  }
}

function onFilterChange(): void {
  page.value = 1
  void load()
}

/** 8 类核心操作里的缺失项（空数组 = AC18 达成；文本型合同会缺 `ocr`）。 */
const missingCoreTypes = computed(() =>
  CORE_LOG_TYPES.filter((item) => !presentTypes.value.includes(item)),
)

function rowClass({ row }: { row: TaskLog }): string {
  return row.log_level === 'error' ? 'row-extract-failed' : ''
}

onMounted(load)
watch(taskId, onFilterChange)
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="card">
      <div class="toolbar">
        <h3 class="card-title" style="margin: 0">任务日志</h3>
        <el-select
          v-model="filterType"
          placeholder="全部类型"
          clearable
          size="small"
          style="width: 160px"
          @change="onFilterChange"
        >
          <el-option v-for="item in ALL_LOG_TYPES" :key="item" :label="logTypeLabel(item)" :value="item" />
        </el-select>
        <el-select
          v-model="filterLevel"
          placeholder="全部级别"
          clearable
          size="small"
          style="width: 140px"
          @change="onFilterChange"
        >
          <el-option v-for="item in LEVELS" :key="item" :label="logLevelLabel(item)" :value="item" />
        </el-select>
        <el-button :icon="Refresh" size="small" :loading="loading" @click="load">刷新</el-button>
        <div class="spacer" />
        <span class="muted">共 {{ total }} 条</span>
      </div>
    </div>

    <div class="card">
      <h3 class="card-title">
        8 类核心操作覆盖情况
        <span class="muted">（FR-LOG-01 / AC18）</span>
      </h3>
      <div class="chips">
        <el-tag
          v-for="item in CORE_LOG_TYPES"
          :key="item"
          :type="presentTypes.includes(item) ? 'success' : 'info'"
          :effect="presentTypes.includes(item) ? 'dark' : 'plain'"
          size="small"
        >
          {{ presentTypes.includes(item) ? '✓' : '—' }} {{ logTypeLabel(item) }}
        </el-tag>
      </div>
      <el-alert
        v-if="missingCoreTypes.length"
        type="info"
        :closable="false"
        style="margin-top: 10px"
        :title="`当前缺少：${missingCoreTypes.map(logTypeLabel).join('、')}`"
      >
        <span class="muted">
          文本型合同没有 OCR 环节，缺 <code>ocr</code> 属预期语义；扫描件走完整闭环时 8 类应齐备。
        </span>
      </el-alert>
      <el-alert
        v-else
        type="success"
        :closable="false"
        style="margin-top: 10px"
        title="8 类核心操作日志齐备（AC18）"
      />
    </div>

    <div class="card">
      <el-table :data="rows" border size="small" :row-class-name="rowClass" empty-text="暂无日志">
        <el-table-column prop="id" label="#" width="70" />
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="级别" width="90">
          <template #default="{ row }">
            <el-tag :type="logLevelTag(row.log_level)" size="small">
              {{ logLevelLabel(row.log_level) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="110">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ logTypeLabel(row.log_type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="内容" min-width="420">
          <template #default="{ row }">
            <div class="evidence">{{ row.log_content }}</div>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        v-model:current-page="page"
        v-model:page-size="size"
        :total="total"
        :page-sizes="[20, 50, 100]"
        layout="total, sizes, prev, pager, next"
        style="margin-top: 12px; justify-content: flex-end"
        @current-change="load"
        @size-change="onFilterChange"
      />
    </div>
  </div>
</template>
