import { defineConfig } from 'vite'
import { resolve } from 'path'
import vue from '@vitejs/plugin-vue'
import cesium from 'vite-plugin-cesium'

// 开发时把 /api、/ws、/output、/basemap 代理到后端(uvicorn 默认 127.0.0.1:8000)
export default defineConfig({
  plugins: [
    vue(),
    cesium(),
    {
      // dev server 下访问根路径直接 302 到地形工具页,与构建产物里 index.html
      // 的 meta-refresh 重定向效果一致(构建产物是静态文件,没有 server 中间件可用)。
      name: 'redirect-root-to-terrain',
      configureServer(server) {
        server.middlewares.use((req, res, next) => {
          if (req.url === '/') {
            res.statusCode = 302
            res.setHeader('Location', '/terrain.html')
            res.end()
            return
          }
          next()
        })
      },
    },
  ],
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
    // 多页入口:index 是根路径重定向页,download 是主下载工具,preview 是成果预览,
    // terrain 是 DEM 地形切片工具(本分支新增、作为默认首页)
    rollupOptions: {
      input: {
        index: resolve(__dirname, 'index.html'),
        download: resolve(__dirname, 'download.html'),
        preview: resolve(__dirname, 'preview.html'),
        terrain: resolve(__dirname, 'terrain.html'),
      },
    },
  },
})
