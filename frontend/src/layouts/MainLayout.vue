<script setup>
/**
 * 登录后的主框架：顶部导航栏 + 下方内容区。
 *
 * 导航栏里的菜单项根据角色动态显示：
 *   普通用户 → 知识库问答、个人中心
 *   管理员   → 额外多出「知识库管理」「统计看板」
 */
import { ElMessageBox } from 'element-plus'
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { logout as logoutApi } from '@/api/auth'
import { useUserStore } from '@/stores/user'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

/** 当前高亮的菜单项，跟着路由走 */
const activeMenu = computed(() => route.path)

/** 角色标签，让用户一眼看出自己是什么身份 */
const roleLabel = computed(() => (userStore.isAdmin ? '管理员' : '普通用户'))

async function handleLogout() {
  try {
    await ElMessageBox.confirm('确定要退出登录吗？', '提示', {
      confirmButtonText: '退出',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return // 用户点了取消
  }

  // 通知一下服务端（目前是空操作，保留是为了将来支持「踢掉所有设备」）
  try {
    await logoutApi()
  } catch {
    // 就算这个请求失败也无所谓，本地凭证一定要清掉
  }

  userStore.clearAuth()
  router.push('/login')
}
</script>

<template>
  <el-container class="layout">
    <el-header class="header">
      <div class="header-left">
        <span class="logo">电商知识库问答系统</span>
        <el-tag v-if="userStore.isAdmin" type="danger" size="small" effect="dark">
          管理员
        </el-tag>
      </div>

      <el-menu
        :default-active="activeMenu"
        mode="horizontal"
        router
        class="nav-menu"
        :ellipsis="false"
      >
        <el-menu-item index="/chat">
          <el-icon><ChatDotRound /></el-icon>
          <span>知识库问答</span>
        </el-menu-item>

        <!-- 仅管理员可见 -->
        <el-menu-item v-if="userStore.isAdmin" index="/knowledge">
          <el-icon><Collection /></el-icon>
          <span>知识库管理</span>
        </el-menu-item>

        <el-menu-item v-if="userStore.isAdmin" index="/dashboard">
          <el-icon><DataAnalysis /></el-icon>
          <span>统计看板</span>
        </el-menu-item>

        <el-menu-item index="/profile">
          <el-icon><User /></el-icon>
          <span>个人中心</span>
        </el-menu-item>
      </el-menu>

      <div class="header-right">
        <el-dropdown @command="handleLogout">
          <span class="user-info">
            <el-icon><Avatar /></el-icon>
            <span class="username">{{ userStore.username }}</span>
            <span class="role">（{{ roleLabel }}）</span>
            <el-icon><ArrowDown /></el-icon>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="logout">
                <el-icon><SwitchButton /></el-icon>
                退出登录
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </el-header>

    <el-main class="main">
      <router-view />
    </el-main>
  </el-container>
</template>

<style scoped>
.layout {
  height: 100%;
}

.header {
  display: flex;
  align-items: center;
  gap: 24px;
  padding: 0 20px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.04);
}

.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.logo {
  font-size: 16px;
  font-weight: 600;
  color: #303133;
  white-space: nowrap;
}

.nav-menu {
  flex: 1;
  border-bottom: none;
}

.header-right {
  flex-shrink: 0;
}

.user-info {
  display: flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  color: #606266;
  outline: none;
}

.username {
  font-weight: 500;
  color: #303133;
}

.role {
  font-size: 12px;
  color: #909399;
}

.main {
  padding: 16px;
  height: calc(100% - 60px);
  overflow: hidden;
}
</style>
