// 独立预览页入口:不与主应用共享 store 实例,单独挂载
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import TDesign from 'tdesign-vue-next'
import 'tdesign-vue-next/es/style/index.css'
import PreviewApp from './preview/PreviewApp.vue'
import { registerProj4Defs } from './utils/crs'
import './style.css'

// 本页不经 App.vue,CGCS2000 定义要自己注册(量测显示平面坐标要用)
registerProj4Defs()

// Pinia 必须装:预览页要用 useServiceStore 读服务列表。
// 这里有自己独立的 Pinia 实例(不与主应用共享状态)——两个页面本就各自
// 独立打开,共享没有意义。漏装的话 useServiceStore() 会抛
// "Cannot read properties of undefined (reading '_s')"。
const app = createApp(PreviewApp)
app.use(createPinia())
app.use(TDesign)
app.mount('#preview')
