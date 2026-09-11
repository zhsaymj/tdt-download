// 独立预览页入口:不与主应用共享 store/路由,单独挂载
import { createApp } from 'vue'
import TDesign from 'tdesign-vue-next'
import 'tdesign-vue-next/es/style/index.css'
import PreviewApp from './preview/PreviewApp.vue'
import { registerProj4Defs } from './utils/crs'
import './style.css'

// 本页不经 App.vue,CGCS2000 定义要自己注册(量测显示平面坐标要用)
registerProj4Defs()

createApp(PreviewApp).use(TDesign).mount('#preview')
