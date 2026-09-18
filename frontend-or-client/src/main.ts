import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'

import App from './App.vue'
import router from './router'
import './styles.css'

createApp(App)
  .use(router)
  // 全量引入 Element Plus：本项目页面量不大，换取"零构建期插件配置"，
  // 少两个 devDependency（unplugin-auto-import / unplugin-vue-components）。
  .use(ElementPlus, { locale: zhCn })
  .mount('#app')
