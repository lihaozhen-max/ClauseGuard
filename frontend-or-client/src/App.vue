<script setup lang="ts">
/** 应用外壳：左侧导航 + 顶部接口密钥入口。窄屏（<1100px）下导航收成图标条。 */
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Key, List, Setting } from '@element-plus/icons-vue'

import { hasApiKey } from './api/client'
import ApiKeyDialog from './components/ApiKeyDialog.vue'
import { useIsNarrow } from './composables/useViewport'

const route = useRoute()
const dialogVisible = ref(false)
const keyReady = ref(hasApiKey())
const { narrow } = useIsNarrow()

/** 侧栏宽度随视口收缩：窄屏只留图标（与 Element Plus 折叠态默认宽度 64px 对齐）。 */
const asideWidth = computed(() => (narrow.value ? '64px' : '196px'))

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
        <span v-if="!narrow" class="brand-sub">合同审批审查系统 · 调用端</span>
      </div>
      <div class="header-actions">
        <el-tag v-if="!keyReady" type="warning" effect="dark" size="small">未配置 X-API-Key</el-tag>
        <el-button size="small" :icon="Key" @click="dialogVisible = true">
          <span v-if="!narrow">接口密钥</span>
        </el-button>
      </div>
    </el-header>

    <el-container>
      <el-aside :width="asideWidth" class="app-aside">
        <el-menu :default-active="activeMenu" router :collapse="narrow" :collapse-transition="false">
          <el-menu-item index="/tasks">
            <el-icon><List /></el-icon>
            <template #title>待办任务</template>
          </el-menu-item>
          <el-menu-item index="/rules">
            <el-icon><Setting /></el-icon>
            <template #title>规则维护</template>
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
