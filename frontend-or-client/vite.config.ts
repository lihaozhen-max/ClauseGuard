import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

/**
 * 开发服务器把 `/api` 反向代理到工具服务（设计 §11 / FIG-02）。
 *
 * 走代理而不是直连 `http://127.0.0.1:8000` 有两个好处：
 * 1. 浏览器侧同源，**不需要**给后端加 CORS 中间件（不扩大后端接口面）；
 * 2. `X-API-Key` 由前端照常带上，鉴权链路与生产一致（FR-SYS-03）。
 */
export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
