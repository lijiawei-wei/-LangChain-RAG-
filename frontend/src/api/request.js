/**
 * axios 实例：统一处理「带上登录凭证」和「令牌过期自动续期」。
 *
 * 这是整个前端最关键的基础设施。它替所有业务代码解决了两个烦人问题：
 *   1. 每个请求都要手动加 Authorization 请求头
 *   2. 登录一小时后令牌过期，用户正用着突然被踢出去
 */
import axios from 'axios'
import { ElMessage } from 'element-plus'

import router from '@/router'
import { useUserStore } from '@/stores/user'

/** 业务请求实例。所有访问后端的调用都走它 */
const request = axios.create({
  baseURL: '/api/v1',
  timeout: 30000, // 30 秒。问答接口比较慢，单独在调用处放宽
})

// ------------------------------------------------------------
// 请求拦截器：发出请求前，自动把登录凭证塞进请求头
// ------------------------------------------------------------
request.interceptors.request.use((config) => {
  const userStore = useUserStore()
  if (userStore.accessToken) {
    config.headers.Authorization = `Bearer ${userStore.accessToken}`
  }
  return config
})

// ------------------------------------------------------------
// 响应拦截器
// ------------------------------------------------------------

/**
 * 正在进行的「刷新令牌」请求。
 *
 * 为什么需要它：用户打开页面时可能同时发出好几个请求，如果 access 恰好过期，
 * 这几个请求会同时收到 401、同时去刷新令牌。并发的刷新请求既浪费又可能互相
 * 覆盖。这里用一个共享的 Promise 保证同一时刻只刷新一次，其它请求等这个
 * 结果就好。
 */
let refreshingPromise = null

request.interceptors.response.use(
  // ---------- 成功响应 ----------
  (response) => {
    const body = response.data

    // 后端统一返回 {code, message, data} 结构，这里把外壳剥掉，
    // 业务代码拿到的直接就是 data，不用每次写 res.data.data
    if (body && typeof body === 'object' && 'code' in body) {
      if (body.code === 0) {
        return body.data
      }
      ElMessage.error(body.message || '请求失败')
      return Promise.reject(new Error(body.message || '请求失败'))
    }

    // 不是标准结构的（比如文件下载）原样返回
    return body
  },

  // ---------- 失败响应 ----------
  async (error) => {
    const { response, config } = error

    // 情况一：压根没收到响应 —— 后端没启动、断网、被防火墙拦了
    if (!response) {
      ElMessage.error('无法连接服务器，请确认后端服务已启动')
      return Promise.reject(error)
    }

    const message =
      response.data?.message || response.data?.detail || `请求失败（${response.status}）`

    // 情况二：401 未登录/令牌过期
    if (response.status === 401) {
      // 排除登录、注册、刷新这几个接口自身返回的 401。
      // 比如「密码错误」也是 401，那属于正常业务失败，不该触发续期逻辑，
      // 否则会变成死循环。
      const url = config?.url || ''
      const isAuthEndpoint = url.includes('/auth/login') || url.includes('/auth/refresh')

      if (isAuthEndpoint || config._retried) {
        return Promise.reject(new Error(message))
      }

      const userStore = useUserStore()

      // 没有刷新令牌，说明确实该重新登录了
      if (!userStore.refreshToken) {
        userStore.clearAuth()
        ElMessage.error('登录已过期，请重新登录')
        router.push('/login')
        return Promise.reject(new Error(message))
      }

      // 标记这个请求已经续过期了，避免失败后再次进入这段逻辑造成死循环
      config._retried = true

      try {
        // 多个请求同时 401 时，共用同一个刷新请求
        if (!refreshingPromise) {
          refreshingPromise = userStore.refreshTokenAction().finally(() => {
            refreshingPromise = null
          })
        }
        await refreshingPromise

        // 刷新成功，用新令牌重放刚才失败的那个请求，用户完全无感
        return request(config)
      } catch (refreshError) {
        // 刷新也失败（刷新令牌也过期了），只能请用户重新登录
        userStore.clearAuth()
        ElMessage.error('登录已过期，请重新登录')
        router.push('/login')
        return Promise.reject(refreshError)
      }
    }

    // 情况三：其它错误（403 无权限、404 不存在、422 参数错、500 服务端错误）
    // 其中 403 单独提示，因为「没权限」和「操作失败」对用户是完全不同的信号
    if (response.status === 403) {
      ElMessage.error(message || '没有权限执行该操作')
    } else {
      ElMessage.error(message)
    }
    return Promise.reject(new Error(message))
  },
)

export default request
