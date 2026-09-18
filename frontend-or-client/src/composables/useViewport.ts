import { computed, onMounted, onUnmounted, ref } from 'vue'

/**
 * 视口宽度是否小于断点（默认 1100px）。
 *
 * 为什么用 JS 而不是纯 CSS：Element Plus 的 `el-aside`/`el-descriptions` 的宽度与列数是**属性**，
 * 会写进行内样式，CSS 媒体查询压不过它。用一个响应式布尔值驱动属性，行为确定、也能被用例断言。
 *
 * 初值直接取 `window.innerWidth`（而不是等 mounted 再改），这样首帧就是对的，
 * 窄屏下不会先闪一下宽布局。
 */
export function useIsNarrow(breakpoint = 1100) {
  const width = ref(typeof window === 'undefined' ? Number.POSITIVE_INFINITY : window.innerWidth)
  const narrow = computed(() => width.value < breakpoint)

  function update(): void {
    width.value = window.innerWidth
  }

  onMounted(() => {
    update()
    window.addEventListener('resize', update)
  })
  onUnmounted(() => window.removeEventListener('resize', update))

  return { narrow, width }
}
