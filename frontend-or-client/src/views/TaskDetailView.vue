<script setup lang="ts">
/**
 * 详情查看模块（FR-UI-02）。
 *
 * 三块内容：审批基本信息、表单数据、合同附件。
 *
 * 附件**只展示元数据**（文件名/类型/大小/SHA-256/下载状态）——合同正文存于后端私有目录，
 * 不挂任何静态路由（FR-SYS-02），页面因此不提供任何直链。
 */
import { computed } from 'vue'

import { useTaskContext } from '../composables/taskContext'
import {
  downloadStatusLabel,
  downloadStatusTag,
  formatSize,
  formatTime,
  taskStatusLabel,
  writeStatusLabel,
} from '../utils/labels'

const { task } = useTaskContext()

/** 表单数据是审批系统给的自由结构，键名原样展示，不猜测语义。 */
const formEntries = computed<Array<[string, string]>>(() => {
  const data = task.value?.form_data ?? {}
  return Object.entries(data).map(([key, value]) => [
    key,
    typeof value === 'string' ? value : JSON.stringify(value, null, 2),
  ])
})
</script>

<template>
  <div v-if="task" class="page">
    <div class="card">
      <h3 class="card-title">审批基本信息</h3>
      <el-descriptions :column="3" border size="small">
        <el-descriptions-item label="审批实例">{{ task.instance_id }}</el-descriptions-item>
        <el-descriptions-item label="审批编号">{{ task.approval_code }}</el-descriptions-item>
        <el-descriptions-item label="申请人">{{ task.applicant_name }}</el-descriptions-item>
        <el-descriptions-item label="申请时间">{{ formatTime(task.apply_time) }}</el-descriptions-item>
        <el-descriptions-item label="审批系统状态">{{ task.current_status || '—' }}</el-descriptions-item>
        <el-descriptions-item label="合同类型">{{ task.contract_type || '—' }}</el-descriptions-item>
        <el-descriptions-item label="任务状态">{{ taskStatusLabel(task.task_status) }}</el-descriptions-item>
        <el-descriptions-item label="回写状态">{{ writeStatusLabel(task.write_status) }}</el-descriptions-item>
        <el-descriptions-item label="重试次数">{{ task.retry_count }}</el-descriptions-item>
        <el-descriptions-item label="创建时间">{{ formatTime(task.created_at) }}</el-descriptions-item>
        <el-descriptions-item label="更新时间">{{ formatTime(task.updated_at) }}</el-descriptions-item>
        <el-descriptions-item label="标题" :span="3">{{ task.approval_title }}</el-descriptions-item>
        <el-descriptions-item v-if="task.error_code" label="错误码">{{ task.error_code }}</el-descriptions-item>
        <el-descriptions-item v-if="task.blocked_stage" label="阻塞阶段">{{ task.blocked_stage }}</el-descriptions-item>
        <el-descriptions-item v-if="task.error_message" label="错误信息" :span="3">
          {{ task.error_message }}
        </el-descriptions-item>
      </el-descriptions>
    </div>

    <div class="card">
      <h3 class="card-title">表单数据</h3>
      <el-empty v-if="formEntries.length === 0" description="审批系统未提供表单数据" :image-size="60" />
      <el-descriptions v-else :column="2" border size="small">
        <el-descriptions-item v-for="[key, value] in formEntries" :key="key" :label="key">
          <span style="white-space: pre-wrap">{{ value }}</span>
        </el-descriptions-item>
      </el-descriptions>
    </div>

    <div class="card">
      <h3 class="card-title">
        合同附件
        <span class="muted">（共 {{ task.attachments.length }} 个）</span>
      </h3>
      <el-table :data="task.attachments" border size="small" empty-text="该审批单没有附件">
        <el-table-column prop="file_name" label="文件名" min-width="220" show-overflow-tooltip />
        <el-table-column prop="file_type" label="类型" width="100" />
        <el-table-column label="大小" width="110">
          <template #default="{ row }">{{ formatSize(row.file_size) }}</template>
        </el-table-column>
        <el-table-column label="SHA-256" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="mono">{{ row.file_checksum || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="下载状态" width="110">
          <template #default="{ row }">
            <el-tag :type="downloadStatusTag(row.download_status)" size="small">
              {{ downloadStatusLabel(row.download_status) }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>
      <p class="muted" style="margin-bottom: 0">
        合同正文保存在后端私有目录，不对外提供静态访问（FR-SYS-02），因此这里只展示元数据。
      </p>
    </div>
  </div>
</template>
