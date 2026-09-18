<script setup lang="ts">
/**
 * 任务详情外壳：载入一次任务详情，提供页头状态与五个模块的切换（FR-UI-02…FR-UI-07）。
 *
 * 页头的任务状态是"活的"——任一模块里触发的解析/审查/重试都会调用 `reload()`
 * 让这里立刻反映最新状态（含 `blocked` 与 `retry_count`）。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { TabPaneName } from 'element-plus'
import { ArrowLeft, RefreshRight } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { describeError } from '../api/client'
import { getTask, retryTask } from '../api/tasks'
import type { TaskDetail } from '../api/types'
import { provideTaskContext } from '../composables/taskContext'
import {
  taskStatusLabel,
  taskStatusTag,
  writeStatusLabel,
  writeStatusTag,
} from '../utils/labels'

const route = useRoute()
const router = useRouter()

const taskId = computed(() => Number(route.params.taskId))
const task = ref<TaskDetail | null>(null)
const loading = ref(false)
const error = ref('')

async function reload(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    task.value = await getTask(taskId.value)
  } catch (cause) {
    error.value = describeError(cause)
    task.value = null
  } finally {
    loading.value = false
  }
}

provideTaskContext({ taskId, task, loading, error, reload })

const activeTab = computed(() => String(route.name ?? 'task-detail'))

function onTabChange(name: TabPaneName): void {
  void router.push({ name: String(name), params: { taskId: taskId.value } })
}

async function onRetry(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '确认执行人工重试？系统会从上次失败的阶段继续跑到完成（IF-20）。',
      '人工重试',
      { type: 'warning', confirmButtonText: '重试', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const result = await retryTask(taskId.value)
    ElMessage.success(
      `重试完成：从 ${result.resumed_stage} 阶段重入，当前状态 ${taskStatusLabel(result.task_status)}`,
    )
    await reload()
  } catch (cause) {
    ElMessage.error(describeError(cause))
    await reload()
  }
}

onMounted(reload)
watch(taskId, reload)
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="card">
      <el-alert v-if="error" type="error" :title="error" :closable="false" show-icon />

      <div class="toolbar" style="margin-bottom: 10px">
        <el-button link :icon="ArrowLeft" @click="router.push({ name: 'tasks' })">返回列表</el-button>
        <span class="mono">task_id = {{ taskId }}</span>
      </div>

      <template v-if="task">
        <div class="toolbar" style="margin-bottom: 12px">
          <strong style="font-size: 16px">{{ task.contract_type || '合同' }}｜{{ task.approval_title }}</strong>
          <el-tag :type="taskStatusTag(task.task_status)" size="small">
            {{ taskStatusLabel(task.task_status) }}
          </el-tag>
          <el-tag :type="writeStatusTag(task.write_status)" size="small" effect="plain">
            回写：{{ writeStatusLabel(task.write_status) }}
          </el-tag>
          <el-tag v-if="task.retry_count > 0" type="info" size="small" effect="plain">
            已重试 {{ task.retry_count }} 次
          </el-tag>
          <div class="spacer" />
          <el-button
            v-if="task.task_status === 'blocked'"
            type="danger"
            size="small"
            :icon="RefreshRight"
            @click="onRetry"
          >
            人工重试（FR-UI-06）
          </el-button>
        </div>

        <el-alert
          v-if="task.task_status === 'blocked'"
          type="warning"
          :closable="false"
          show-icon
          style="margin-bottom: 12px"
        >
          <template #title>
            任务已阻塞于 <b>{{ task.blocked_stage }}</b> 阶段：{{ task.error_code }}
          </template>
          <div class="muted">{{ task.error_message }}</div>
        </el-alert>

        <el-tabs :model-value="activeTab" @tab-change="onTabChange">
          <el-tab-pane label="详情查看" name="task-detail" />
          <el-tab-pane label="解析结果" name="task-parse" />
          <el-tab-pane label="规则命中" name="task-review" />
          <el-tab-pane label="结果处理" name="task-result" />
          <el-tab-pane label="任务日志" name="task-logs" />
        </el-tabs>
      </template>

      <el-empty v-else-if="!loading && !error" description="任务不存在或已被清理" />
    </div>

    <router-view />
  </div>
</template>
