<script setup lang="ts">
/**
 * 接口密钥对话框（FR-SYS-03）。
 *
 * `X-API-Key` 取自构建期变量 `VITE_INTERNAL_API_KEY`，也可在这里临时填写并落在
 * localStorage —— 演示时换后端不必重新构建。密钥只用于发请求，不做任何其他用途。
 */
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { getApiKey, setApiKey } from '../api/client'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'saved', value: string): void
}>()

const draft = ref('')

watch(
  () => props.modelValue,
  (visible) => {
    if (visible) draft.value = getApiKey()
  },
)

function close(): void {
  emit('update:modelValue', false)
}

function save(): void {
  setApiKey(draft.value)
  emit('saved', draft.value.trim())
  ElMessage.success(draft.value.trim() ? '接口密钥已保存到本机浏览器' : '已清除接口密钥')
  close()
}
</script>

<template>
  <el-dialog
    :model-value="props.modelValue"
    title="内部接口密钥"
    width="480px"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <p class="muted">
      所有内部接口都要求请求头 <code>X-API-Key</code>（值与后端 <code>.env</code> 的
      <code>INTERNAL_API_KEY</code> 一致，FR-SYS-03）。此处填写的值只保存在本机浏览器。
    </p>
    <el-input
      v-model="draft"
      type="password"
      show-password
      placeholder="粘贴 INTERNAL_API_KEY"
      clearable
      @keyup.enter="save"
    />
    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button type="primary" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>
