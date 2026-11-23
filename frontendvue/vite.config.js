import { defineConfig } from 'vite'
import { resolve } from 'path'
import vue from '@vitejs/plugin-vue'
import cesium from 'vite-plugin-cesium'

// 开发时把 /api、/ws、/output、/basemap 代理到后端(uvicorn 默认 127.0.0.1:8000)
export default defineConfig({
  plugins: [vue(), cesium()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
      '/output': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/basemap': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // 多页入口:主下载页 index + 独立预览页 preview + 独立地形切片页 terrain
    // terrain 是并列的第三个入口,不改动 index 的默认首页地位:主工具仍在根路径,
    // 地形工具走 /terrain.html。需要出"地形切片版"独立安装包时,由打包配置决定
    // 启动哪个入口,不在构建产物里做重定向。
    rollupOptions: {
      input: {
        index: resolve(__dirname, 'index.html'),
        preview: resolve(__dirname, 'preview.html'),
        terrain: resolve(__dirname, 'terrain.html'),
      },
    },
  },
})
