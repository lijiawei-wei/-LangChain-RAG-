/**
 * 用户状态：管登录凭证、当前用户信息、登录登出动作。
 *
 * 凭证存在 localStorage 里，所以关闭浏览器再打开还是登录状态。
 * 安全提醒：localStorage 能被 JS 读到，理论上存在 XSS 窃取风险。
 * 生产环境更稳妥的做法是把刷新令牌放进 HttpOnly Cookie，本项目为便于
 * 讲解和调试采用 localStorage。
 */
import axios from 'axios'
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

const STORAGE_KEY = 'rag_auth'

export const useUserStore = defineStore('user', () => {
  // 初始化时从 localStorage 恢复上次的登录状态
  const saved = (() => {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}')
    } catch {
      // 存的内容被改坏了就当作没登录，不要让整个页面崩掉
      return {}
    }
  })()

  const accessToken = ref(saved.accessToken || '')
  const refreshToken = ref(saved.refreshToken || '')
  const user = ref(saved.user || null)

  /** 是否已登录 */
  const isLoggedIn = computed(() => Boolean(accessToken.value))

  /** 是否管理员。前端靠它决定显示不显示「知识库管理」入口 */
  const isAdmin = computed(() => user.value?.role === 'admin')

  const username = computed(() => user.value?.username || '')

  /** 把当前状态写回 localStorage */
  function persist() {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        accessToken: accessToken.value,
        refreshToken: refreshToken.value,
        user: user.value,
      }),
    )
  }

  /** 保存登录/刷新接口返回的令牌与用户信息 */
  function setAuth(payload) {
    accessToken.value = payload.access_token
    refreshToken.value = payload.refresh_token
    user.value = payload.user
    persist()
  }

  /** 清空登录状态（登出、或令牌彻底失效时调用） */
  function clearAuth() {
    accessToken.value = ''
    refreshToken.value = ''
    user.value = null
    localStorage.removeItem(STORAGE_KEY)
  }

  /**
   * 用刷新令牌换新的访问令牌。
   *
   * 注意这里用的是「原始的 axios」而不是我们封装过的 request 实例。
   * 原因：request 实例带 401 拦截器，而这个请求本身就可能返回 401，
   * 用 request 会导致拦截器递归调用自己，陷入死循环。
   */
  async function refreshTokenAction() {
    if (!refreshToken.value) {
      throw new Error('没有刷新令牌')
    }

    const { data } = await axios.post('/api/v1/auth/refresh', {
      refresh_token: refreshToken.value,
    })

    if (data?.code !== 0 || !data?.data) {
      throw new Error(data?.message || '刷新令牌失败')
    }

    setAuth(data.data)
    return data.data
  }

  return {
    accessToken,
    refreshToken,
    user,
    isLoggedIn,
    isAdmin,
    username,
    setAuth,
    clearAuth,
    refreshTokenAction,
  }
})
