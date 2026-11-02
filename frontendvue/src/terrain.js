// 独立地形工具页入口:不与主应用共享 store 实例,单独挂载
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import TDesign from 'tdesign-vue-next'
import 'tdesign-vue-next/es/style/index.css'
import 'ol/ol.css'
import TerrainApp from './terrain/TerrainApp.vue'
import './style.css'

const app = createApp(TerrainApp)
app.use(createPinia())
app.use(TDesign)
app.mount('#terrain')
