/**
 * 统计接口。
 *
 * 分两组，权限不同：
 *   /stats/me      —— 任何登录用户，看自己的
 *   /admin/stats/* —— 仅管理员，看全站
 * 普通用户调用管理员那组会拿到 403，页面上的菜单入口也已经隐藏了，
 * 前端这里不需要额外做权限判断。
 */
import request from './request'

/** 我的问答统计（个人中心用）。 */
export function getMyStats() {
  return request.get('/stats/me')
}

/** 全站总览：用户数、文档数、问答量、缓存命中率、平均延迟等。 */
export function getOverview() {
  return request.get('/admin/stats/overview')
}

/** 服务运行时状态：缓存占用、正在进行的问答数。重启服务会归零。 */
export function getRuntimeStats() {
  return request.get('/admin/stats/runtime')
}

/**
 * 问答趋势。
 * @param {number} days 往回看多少天，1~90
 */
export function getTrend(days = 7) {
  return request.get('/admin/stats/trend', { params: { days } })
}

/** 高频问题排行。 */
export function getTopQuestions(limit = 10) {
  return request.get('/admin/stats/top-questions', { params: { limit } })
}

/**
 * 最近的反馈记录。
 * @param {number|null} rating 传 1 只看点赞、-1 只看点踩、不传看全部
 */
export function getRecentFeedback(rating = null, limit = 20) {
  return request.get('/admin/stats/recent-feedback', {
    params: { rating: rating ?? undefined, limit },
  })
}
