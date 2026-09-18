<script setup lang="ts">
/** 应用外壳：左侧导航 + 顶部接口密钥入口。 */
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Key, List, Setting } from '@element-plus/icons-vue'

import { hasApiKey } from './api/client'
import ApiKeyDialog from './components/ApiKeyDialog.vue'

const route = useRoute()
const dialogVisible = ref(false)
const keyReady = ref(hasApiKey())

/** 子路由（/tasks/:id/detail…）统一高亮"待办任务"。 */
const activeMenu = computed(() => (route.path.startsWith('/rules') ? '/rules' : '/tasks'))

function onSaved(): void {
  keyReady.value = hasApiKey()
}

// 没配密钥时直接弹出：否则用户面对的是一串 401，很难猜到原因
if (!keyReady.value) dialogVisible.value = true
</script>

<template>
  <el-container class="app-shell">
    <el-header class="app-header">
      <div class="brand">
        <span class="brand-mark">ClauseGuard</span>
        <span class="brand-sub">合同审批审查系统 · 调用端</span>
      </div>
      <div class="header-actions">
        <el-tag v-if="!keyReady" type="warning" effect="dark" size="small">未配置 X-API-Key</el-tag>
        <el-button size="small" :icon="Key" @click="dialogVisible = true">接口密钥</el-button>
      </div>
    </el-header>

    <el-container>
      <el-aside width="196px" class="app-aside">
        <el-menu :default-active="activeMenu" router>
          <el-menu-item index="/tasks">
            <el-icon><List /></el-icon>
            <span>待办任务</span>
          </el-menu-item>
          <el-menu-item index="/rules">
            <el-icon><Setting /></el-icon>
            <span>规则维护</span>
          </el-menu-item>
        </el-menu>
      </el-aside>

      <el-main class="app-main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>

  <ApiKeyDialog v-model="dialogVisible" @saved="onSaved" />
</template>
