<script setup>
/** 登录页 */
import { ElMessage } from 'element-plus'
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { login } from '@/api/auth'
import { useUserStore } from '@/stores/user'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const formRef = ref()
const loading = ref(false)

const form = reactive({
  username: '',
  password: '',
})

const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

async function handleSubmit() {
  // 先让 Element Plus 做一遍表单校验，不通过就不发请求
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  loading.value = true
  try {
    const data = await login(form.username, form.password)
    userStore.setAuth(data)
    ElMessage.success(`欢迎回来，${data.user.username}`)

    // 如果之前是被守卫拦下来的，登录后跳回原本要去的页面
    const redirect = route.query.redirect
    router.push(typeof redirect === 'string' ? redirect : '/chat')
  } catch {
    // 具体错误提示已由 request.js 的拦截器统一弹出，这里不用重复提示
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="auth-page">
    <div class="auth-card">
      <h1 class="auth-title">电商知识库问答系统</h1>
      <p class="auth-subtitle">基于 LangChain 的 RAG 智能问答</p>

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        size="large"
        @keyup.enter="handleSubmit"
      >
        <el-form-item prop="username">
          <el-input v-model="form.username" placeholder="请输入用户名" clearable>
            <template #prefix>
              <el-icon><User /></el-icon>
            </template>
          </el-input>
        </el-form-item>

        <el-form-item prop="password">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="请输入密码"
            show-password
          >
            <template #prefix>
              <el-icon><Lock /></el-icon>
            </template>
          </el-input>
        </el-form-item>

        <el-form-item>
          <el-button
            type="primary"
            class="auth-submit"
            :loading="loading"
            @click="handleSubmit"
          >
            登 录
          </el-button>
        </el-form-item>
      </el-form>

      <div class="auth-footer">
        还没有账号？
        <router-link to="/register">立即注册</router-link>
      </div>

      <el-alert
        title="管理员默认账号：admin / 123456"
        type="info"
        :closable="false"
        show-icon
        class="demo-tip"
      />
    </div>
  </div>
</template>

<style scoped>
.demo-tip {
  margin-top: 16px;
}
</style>
