// 独立预览页入口:不与主应用共享 store/路由,单独挂载
import { createApp } from 'vue'
import TDesign from 'tdesign-vue-next'
import 'tdesign-vue-next/es/style/index.css'
import PreviewApp from './preview/PreviewApp.vue'
import './style.css'

createApp(PreviewApp).use(TDesign).mount('#preview')
