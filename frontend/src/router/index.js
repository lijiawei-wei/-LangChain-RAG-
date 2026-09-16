/**
 * 路由配置与访问守卫。
 *
 * 守卫做两件事：
 *   1. 没登录的用户只能待在登录页/注册页
 *   2. 普通用户访问不了管理员页面（即使手动敲网址也会被挡回来）
 *
 * 注意：前端守卫只是「体验优化」，让用户看不到用不了的入口。
 * 真正的权限控制在后端（require_admin 依赖），因为前端代码用户能改。
 */
import { createRouter, createWebHistory } from 'vue-router'

import { useUserStore } from '@/stores/user'

const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/Login.vue'),
    meta: { public: true, title: '登录' },
  },
  {
    path: '/register',
    name: 'register',
    component: () => import('@/views/Register.vue'),
    meta: { public: true, title: '注册' },
  },
  {
    path: '/',
    component: () => import('@/layouts/MainLayout.vue'),
    redirect: '/chat',
    children: [
      {
        path: 'chat',
        name: 'chat',
        component: () => import('@/views/Chat.vue'),
        meta: { title: '知识库问答' },
      },
      {
        path: 'knowledge',
        name: 'knowledge',
        component: () => import('@/views/Knowledge.vue'),
        meta: { title: '知识库管理', adminOnly: true },
      },
      {
        path: 'dashboard',
        name: 'dashboard',
        component: () => import('@/views/Dashboard.vue'),
        meta: { title: '统计看板', adminOnly: true },
      },
      {
        path: 'profile',
        name: 'profile',
        component: () => import('@/views/Profile.vue'),
        meta: { title: '个人中心' },
      },
    ],
  },
  {
    // 兜底：访问不存在的路径时回首页，而不是显示一片空白
    path: '/:pathMatch(.*)*',
    redirect: '/chat',
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to) => {
  const userStore = useUserStore()

  // 顺手把页面标题改成「当前页面名 - 系统名」，浏览器标签页更好认
  document.title = to.meta.title ? `${to.meta.title} - 电商知识库问答系统` : '电商知识库问答系统'

  // 已登录的用户不该再看到登录/注册页
  if (to.meta.public) {
    return userStore.isLoggedIn ? { name: 'chat' } : true
  }

  // 未登录 → 去登录页，并记住原本想去哪，登录后自动跳回去
  if (!userStore.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  // 普通用户访问管理员页面 → 挡回问答页
  if (to.meta.adminOnly && !userStore.isAdmin) {
    return { name: 'chat' }
  }

  return true
})

export default router
