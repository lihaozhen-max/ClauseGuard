<script setup lang="ts">
/**
 * 结果处理模块（FR-UI-05 + FR-UI-06）。
 *
 * 展示评论内容、回写状态、回写时间与回写错误，并提供回写/重试操作；
 * `blocked` 任务额外给出人工重试入口（IF-20 / AC17）。
 *
 * 回写语义（FR-COM-04 / ST-01-04）：回写失败**不会**删除审查结果，任务也**不会**从 `done`
 * 回退，所以这里把"回写状态"与"任务状态"分开呈现，避免误读为任务失败。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { Promotion, Refresh, RefreshRight, VideoPlay } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { ApiError, describeError } from '../api/client'
import { getCommentLogs, getReviewResult, retryTask, triggerReview, writeComment } from '../api/tasks'
import type { CommentLog, ReviewPipelineResponse } from '../api/types'
import { useTaskContext } from '../composables/taskContext'
import { formatTime, taskStatusLabel, writeStatusLabel, writeStatusTag } from '../utils/labels'

const { taskId, task, reload: reloadTask } = useTaskContext()

const loading = ref(false)
const acting = ref(false)
const result = ref<ReviewPipelineResponse | null>(null)
const logs = ref<CommentLog[]>([])
const errorMessage = ref('')

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ''
  try {
    result.value = await getReviewResult(taskId.value)
  } catch (error) {
    result.value = null
    if (!(error instanceof ApiError && error.code === 'PARSE_REQUIRED')) {
      errorMessage.value = describeError(error)
    }
  }
  try {
    logs.value = (await getCommentLogs(taskId.value)).items
  } catch (error) {
    // 回写历史取不到不影响主流程展示，只在控制台留痕
    console.warn('回写历史加载失败', describeError(error))
  }
  loading.value = false
}

async function onReview(): Promise<void> {
  acting.value = true
  try {
    result.value = await triggerReview(taskId.value)
    ElMessage.success('审查完成，已生成评论正文')
    await reloadTask()
    await load()
  } catch (error) {
    ElMessage.error(describeError(error))
    await reloadTask()
  } finally {
    acting.value = false
  }
}

async function onWriteComment(): Promise<void> {
  acting.value = true
  try {
    const outcome = await writeComment(taskId.value)
    if (outcome.write_status === 'success') {
      ElMessage.success(
        outcome.duplicate
          ? `已回写过，直接返回既有结果（备注号 ${outcome.remark_id}，FR-COM-06 幂等）`
          : `回写成功，备注号 ${outcome.remark_id}`,
      )
    } else {
      ElMessage.warning(`回写失败：${outcome.write_response_text || '未返回原因'}`)
    }
    await reloadTask()
    await load()
  } catch (error) {
    ElMessage.error(describeError(error))
    await reloadTask()
    await load()
  } finally {
    acting.value = false
  }
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
  acting.value = true
  try {
    const outcome = await retryTask(taskId.value)
    ElMessage.success(
      `重试完成：从 ${outcome.resumed_stage} 阶段重入，任务状态 ${taskStatusLabel(outcome.task_status)}，` +
        `累计重试 ${outcome.retry_count} 次`,
    )
    await reloadTask()
    await load()
  } catch (error) {
    ElMessage.error(describeError(error))
    await reloadTask()
    await load()
  } finally {
    acting.value = false
  }
}

/** 最近一次成功的回写（用于"回写时间"）。 */
const lastSuccess = computed<CommentLog | null>(
  () => logs.value.find((item) => item.write_status === 'success') ?? null,
)

/** 最近一次失败的原因（用于"回写错误"）。 */
const lastFailure = computed<CommentLog | null>(
  () => logs.value.find((item) => item.write_status === 'failed') ?? null,
)

onMounted(load)
watch(taskId, load)
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="card">
      <div class="toolbar">
        <h3 class="card-title" style="margin: 0">结果处理</h3>
        <el-tag v-if="task" :type="writeStatusTag(task.write_status)" size="small">
          回写：{{ writeStatusLabel(task.write_status) }}
        </el-tag>
        <el-tag v-if="task" size="small" effect="plain" type="info">
          任务：{{ taskStatusLabel(task.task_status) }}
        </el-tag>
        <div class="spacer" />
        <el-button :icon="Refresh" size="small" :loading="loading" @click="load">刷新</el-button>
        <el-button
          v-if="!result"
          type="primary"
          size="small"
          :icon="VideoPlay"
          :loading="acting"
          @click="onReview"
        >
          触发审查（IF-16）
        </el-button>
        <el-button
          v-else
          type="primary"
          size="small"
          :icon="Promotion"
          :loading="acting"
          @click="onWriteComment"
        >
          {{ task?.write_status === 'failed' ? '重试回写' : '回写评论' }}（IF-17）
        </el-button>
        <el-button
          v-if="task?.task_status === 'blocked'"
          type="danger"
          size="small"
          :icon="RefreshRight"
          :loading="acting"
          @click="onRetry"
        >
          人工重试（IF-20）
        </el-button>
      </div>
    </div>

    <el-alert v-if="errorMessage" type="error" :title="errorMessage" :closable="false" show-icon />

    <div class="card">
      <h3 class="card-title">回写概览</h3>
      <el-descriptions :column="3" border size="small">
        <el-descriptions-item label="回写状态">
          <el-tag v-if="task" :type="writeStatusTag(task.write_status)" size="small">
            {{ writeStatusLabel(task.write_status) }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="回写时间">
          {{ formatTime(lastSuccess?.created_at) }}
        </el-descriptions-item>
        <el-descriptions-item label="备注号（remark_id）">
          <span class="mono">{{ lastSuccess?.remark_id || '—' }}</span>
        </el-descriptions-item>
        <el-descriptions-item v-if="lastFailure" label="回写错误" :span="3">
          <span style="color: var(--cg-risk-high)">
            {{ lastFailure.write_response_text || '审批系统未返回原因' }}
          </span>
        </el-descriptions-item>
      </el-descriptions>
      <p class="muted" style="margin-bottom: 0">
        回写失败只置 <code>write_status=failed</code>，不删除审查结果，任务也不会从 <code>done</code> 回退
        （FR-COM-04 / ST-01-04）。
      </p>
    </div>

    <div class="card">
      <h3 class="card-title">
        评论内容
        <span class="muted">（§4.6.1 模板产物，将写入审批系统评论区）</span>
      </h3>
      <el-empty
        v-if="!result?.comment_text"
        description="尚无评论正文，请先触发审查"
        :image-size="60"
      />
      <pre v-else class="pre-block">{{ result.comment_text }}</pre>
    </div>

    <div class="card">
      <h3 class="card-title">
        回写历史
        <span class="muted">（共 {{ logs.length }} 条，IF-18）</span>
      </h3>
      <el-table :data="logs" border size="small" empty-text="尚无回写记录">
        <el-table-column prop="id" label="#" width="70" />
        <el-table-column label="回写状态" width="110">
          <template #default="{ row }">
            <el-tag :type="writeStatusTag(row.write_status)" size="small">
              {{ writeStatusLabel(row.write_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="备注号" width="130">
          <template #default="{ row }">
            <span class="mono">{{ row.remark_id || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="回写时间" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="幂等键" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="mono">{{ row.idempotency_key }}</span>
          </template>
        </el-table-column>
        <el-table-column label="审批系统响应" min-width="220">
          <template #default="{ row }">
            <div class="evidence">{{ row.write_response_text || '—' }}</div>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>
