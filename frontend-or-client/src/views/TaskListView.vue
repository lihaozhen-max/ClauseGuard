<script setup lang="ts">
/**
 * 待办调用模块（FR-UI-01）。
 *
 * 展示：审批编号、标题、申请人、申请时间、附件数量、任务状态；
 * 另提供 IF-10 拉取入口、状态筛选与 `blocked` 任务的重试入口（FR-UI-06）。
 */
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Download, Refresh, RefreshRight, Search } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { describeError } from '../api/client'
import { listTasks, pullTasks, retryTask } from '../api/tasks'
import type { TaskListItem, TaskStatus } from '../api/types'
import { formatTime, taskStatusLabel, taskStatusTag, writeStatusLabel, writeStatusTag } from '../utils/labels'

const router = useRouter()

const STATUS_OPTIONS: TaskStatus[] = ['pending', 'parsing', 'reviewing', 'blocked', 'done']

const loading = ref(false)
const pulling = ref(false)
const rows = ref<TaskListItem[]>([])
const total = ref(0)
const pullLimit = ref(20)
const query = reactive({ status: '' as TaskStatus | '', page: 1, size: 20 })

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await listTasks({ status: query.status, page: query.page, size: query.size })
    rows.value = data.items
    total.value = data.total
  } catch (error) {
    ElMessage.error(describeError(error))
  } finally {
    loading.value = false
  }
}

async function onPull(): Promise<void> {
  pulling.value = true
  try {
    const result = await pullTasks(pullLimit.value)
    ElMessage.success(
      `拉取完成：新增 ${result.created_count} 条、更新 ${result.updated_count} 条（共 ${result.items.length} 条待办）`,
    )
    query.page = 1
    await load()
  } catch (error) {
    ElMessage.error(describeError(error))
  } finally {
    pulling.value = false
  }
}

function openTask(row: TaskListItem): void {
  void router.push({ name: 'task-detail', params: { taskId: row.task_id } })
}

async function onRetry(row: TaskListItem): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认对任务 ${row.task_id}（${row.instance_id}）执行人工重试？将从上一次失败的阶段继续跑到完成。`,
      '人工重试',
      { type: 'warning', confirmButtonText: '重试', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const result = await retryTask(row.task_id)
    ElMessage.success(
      `重试完成：从 ${result.resumed_stage} 阶段重入，当前状态 ${taskStatusLabel(result.task_status)}，` +
        `累计重试 ${result.retry_count} 次`,
    )
    await load()
  } catch (error) {
    ElMessage.error(describeError(error))
  }
}

function onSearch(): void {
  query.page = 1
  void load()
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="card">
      <h3 class="card-title">待办调用</h3>
      <div class="toolbar">
        <el-input-number v-model="pullLimit" :min="1" :max="200" size="small" />
        <el-button type="primary" :icon="Download" :loading="pulling" @click="onPull">
          拉取待办（IF-10）
        </el-button>
        <el-select v-model="query.status" placeholder="全部状态" clearable size="small" style="width: 150px">
          <el-option
            v-for="item in STATUS_OPTIONS"
            :key="item"
            :label="taskStatusLabel(item)"
            :value="item"
          />
        </el-select>
        <el-button :icon="Search" size="small" @click="onSearch">查询</el-button>
        <el-button :icon="Refresh" size="small" :loading="loading" @click="load">刷新</el-button>
        <div class="spacer" />
        <span class="muted">共 {{ total }} 条</span>
      </div>
    </div>

    <div class="card">
      <el-table v-loading="loading" :data="rows" border size="small" empty-text="暂无任务，先点『拉取待办』">
        <el-table-column prop="task_id" label="任务" width="72" />
        <el-table-column prop="approval_code" label="审批编号" width="140" />
        <el-table-column prop="approval_title" label="标题" min-width="200" show-overflow-tooltip />
        <el-table-column prop="applicant_name" label="申请人" width="100" />
        <el-table-column label="申请时间" width="160">
          <template #default="{ row }">{{ formatTime(row.apply_time) }}</template>
        </el-table-column>
        <el-table-column label="附件数" width="80" align="center">
          <template #default="{ row }">{{ row.attachment_count }}</template>
        </el-table-column>
        <el-table-column label="任务状态" width="100">
          <template #default="{ row }">
            <el-tag :type="taskStatusTag(row.task_status)" size="small">
              {{ taskStatusLabel(row.task_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="回写状态" width="110">
          <template #default="{ row }">
            <el-tag :type="writeStatusTag(row.write_status)" size="small" effect="plain">
              {{ writeStatusLabel(row.write_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="170" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openTask(row)">详情</el-button>
            <el-button
              v-if="row.task_status === 'blocked'"
              link
              type="danger"
              size="small"
              :icon="RefreshRight"
              @click="onRetry(row)"
            >
              人工重试
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        v-model:current-page="query.page"
        v-model:page-size="query.size"
        :total="total"
        :page-sizes="[10, 20, 50]"
        layout="total, sizes, prev, pager, next"
        style="margin-top: 12px; justify-content: flex-end"
        @current-change="load"
        @size-change="onSearch"
      />
    </div>
  </div>
</template>
