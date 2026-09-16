import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [vue()],

  resolve: {
    alias: {
      // 让代码里可以用 @/xxx 代替一长串相对路径 ../../..
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },

  server: {
    port: 5173,

    // 监听所有网卡。**这一行不能省**，原因很隐蔽：
    //
    // 不写 host 时，Vite 只绑定在 localhost 上，而 localhost 具体解析成
    // IPv4(127.0.0.1) 还是 IPv6(::1) 由操作系统决定。
    // 实测这台机器上它只绑到了 IPv6，于是出现：
    //     http://localhost:5173   ✅ 能打开
    //     http://127.0.0.1:5173   ❌ 连不上
    // 演示时有人手动敲 IP 就会当场卡住，而且很难想到是这个原因。
    //
    // 加上之后两个地址都能用，顺带还能用手机或另一台电脑连过来看
    // （局域网内访问 http://本机IP:5173）。
    host: true,

    // 开发时把 /api 开头的请求转发给后端，这样前端代码里直接写 /api/v1/xxx 即可。
    // 好处：浏览器眼里前后端同源，不会有跨域问题，也就不需要处理 CORS。
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
