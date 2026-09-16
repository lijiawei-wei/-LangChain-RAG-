/**
 * 认证相关接口。
 *
 * 每个函数对应后端的一个网址。因为 request.js 的响应拦截器已经把
 * {code, message, data} 的外壳剥掉了，这里拿到的直接就是 data。
 */
import request from './request'

/** 注册新用户 */
export function register(username, password) {
  return request.post('/auth/register', { username, password })
}

/** 登录，返回 { access_token, refresh_token, expires_in, user } */
export function login(username, password) {
  return request.post('/auth/login', { username, password })
}

/** 登出（服务端无状态，实际清理由前端 store 完成） */
export function logout() {
  return request.post('/auth/logout')
}

/** 获取当前登录用户信息 */
export function getMe() {
  return request.get('/auth/me')
}

/** 修改密码，返回新的令牌 */
export function changePassword(oldPassword, newPassword) {
  return request.put('/auth/password', {
    old_password: oldPassword,
    new_password: newPassword,
  })
}
