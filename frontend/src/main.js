/**
 * 前端总入口：创建 Vue 应用，装上需要的插件，然后挂载到页面上。
 */
import * as ElementPlusIcons from '@element-plus/icons-vue'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import router from './router'

import 'element-plus/dist/index.css'
import './styles/main.css'

const app = createApp(App)

// 把 Element Plus 的所有图标注册成全局组件，
// 这样模板里可以直接写 <el-icon><User /></el-icon>，不用每个文件都 import 一遍
for (const [name, component] of Object.entries(ElementPlusIcons)) {
  app.component(name, component)
}

app.use(createPinia()) // 状态管理
app.use(router) // 路由
app.use(ElementPlus, { locale: zhCn }) // UI 组件库，用中文语言包

app.mount('#app')
