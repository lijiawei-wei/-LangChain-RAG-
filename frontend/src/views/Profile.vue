<script setup>
/** 个人中心：查看账号信息 + 我的问答统计 + 修改密码 */
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { changePassword } from '@/api/auth'
import { getMyStats } from '@/api/stats'
import { useUserStore } from '@/stores/user'
import { formatDuration, formatNumber, formatTokens } from '@/utils/format'

const userStore = useUserStore()
const formRef = ref()
const loading = ref(false)

// ---------- 我的问答统计 ----------
const loadingStats = ref(false)
const myStats = ref(null)

onMounted(async () => {
  loadingStats.value = true
  try {
    myStats.value = await getMyStats()
  } catch {
    // 拉不到统计不影响改密码，静默处理
  } finally {
    loadingStats.value = false
  }
})

/**
 * 统计项。没拿到数据时全部显示"—"而不是"0"：
 * 0 会被读成"我真的一次都没问过"，而"—"表达的是"还没加载出来"。
 */
const myStatItems = computed(() => {
  const s = myStats.value
  return [
    { label: '提问次数', value: s ? formatNumber(s.qa_total) : '—' },
    { label: '会话数', value: s ? formatNumber(s.conversation_count) : '—' },
    { label: 'Token 消耗', value: s ? formatTokens(s.total_tokens) : '—' },
    {
      label: '平均响应',
      value: s ? formatDuration(s.avg_latency_ms) : '—',
    },
    {
      label: '平均首字延迟',
      // 一次缓存命中都没有过的用户，这个值会是 0（没有样本）。
      // formatDuration 会把 0 显示成"—"，语义正好也对
      value: s ? formatDuration(s.avg_first_token_ms) : '—',
    },
    {
      label: '缓存命中率',
      value: s ? `${s.cache_hit_rate}%` : '—',
    },
    { label: '我点的赞', value: s ? formatNumber(s.likes) : '—' },
    { label: '我点的踩', value: s ? formatNumber(s.dislikes) : '—' },
  ]
})

const form = reactive({
  oldPassword: '',
  newPassword: '',
  confirmPassword: '',
})

const roleLabel = computed(() => (userStore.isAdmin ? '管理员' : '普通用户'))

/** 新密码不能和当前密码一样 */
function validateNewPassword(rule, value, callback) {
  if (value && value === form.oldPassword) {
    callback(new Error('新密码不能与当前密码相同'))
  } else {
    callback()
  }
}

/** 两次输入必须一致 */
function validateConfirm(rule, value, callback) {
  if (value !== form.newPassword) {
    callback(new Error('两次输入的新密码不一致'))
  } else {
    callback()
  }
}

const rules = {
  oldPassword: [{ required: true, message: '请输入当前密码', trigger: 'blur' }],
  newPassword: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 6, max: 128, message: '密码长度至少 6 位', trigger: 'blur' },
    { validator: validateNewPassword, trigger: 'blur' },
  ],
  confirmPassword: [
    { required: true, message: '请再次输入新密码', trigger: 'blur' },
    { validator: validateConfirm, trigger: 'blur' },
  ],
}

/** 把 ISO 时间字符串转成好读的格式 */
function formatTime(value) {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString('zh-CN', { hour12: false })
}

async function handleSubmit() {
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  loading.value = true
  try {
    // 后端改密成功后会下发一套新令牌（因为旧令牌已随改密全部作废），
    // 这里立刻用新令牌覆盖本地存储，用户当前会话就能无缝继续
    const data = await changePassword(form.oldPassword, form.newPassword)
    userStore.setAuth(data)

    ElMessage.success('密码修改成功')
    formRef.value.resetFields()
  } catch {
    // 错误提示由拦截器统一处理
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="profile-page">
    <el-row :gutter="16">
      <!-- 左：账号信息 -->
      <el-col :xs="24" :md="10">
        <el-card shadow="never">
          <template #header>
            <span class="card-title">账号信息</span>
          </template>

          <el-descriptions :column="1" border>
            <el-descriptions-item label="用户名">
              {{ userStore.username }}
            </el-descriptions-item>
            <el-descriptions-item label="角色">
              <el-tag :type="userStore.isAdmin ? 'danger' : 'primary'" size="small">
                {{ roleLabel }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="注册时间">
              {{ formatTime(userStore.user?.created_at) }}
            </el-descriptions-item>
            <el-descriptions-item label="上次登录">
              {{ formatTime(userStore.user?.last_login_at) }}
            </el-descriptions-item>
          </el-descriptions>

          <el-alert
            v-if="userStore.isAdmin"
            title="你是管理员，可以进入「知识库管理」上传商品资料，并在「统计看板」查看系统运行数据。"
            type="success"
            :closable="false"
            show-icon
            class="role-tip"
          />
          <el-alert
            v-else
            title="普通用户可以使用知识库问答功能，知识库内容由管理员维护。"
            type="info"
            :closable="false"
            show-icon
            class="role-tip"
          />
        </el-card>
      </el-col>

      <!-- 右：修改密码 -->
      <el-col :xs="24" :md="14">
        <el-card shadow="never">
          <template #header>
            <span class="card-title">修改密码</span>
          </template>

          <el-form
            ref="formRef"
            :model="form"
            :rules="rules"
            label-width="100px"
            style="max-width: 460px"
          >
            <el-form-item label="当前密码" prop="oldPassword">
              <el-input
                v-model="form.oldPassword"
                type="password"
                placeholder="请输入当前密码"
                show-password
              />
            </el-form-item>

            <el-form-item label="新密码" prop="newPassword">
              <el-input
                v-model="form.newPassword"
                type="password"
                placeholder="至少 6 位"
                show-password
              />
            </el-form-item>

            <el-form-item label="确认新密码" prop="confirmPassword">
              <el-input
                v-model="form.confirmPassword"
                type="password"
                placeholder="请再次输入新密码"
                show-password
              />
            </el-form-item>

            <el-form-item>
              <el-button type="primary" :loading="loading" @click="handleSubmit">
                确认修改
              </el-button>
              <el-button @click="formRef.resetFields()">重置</el-button>
            </el-form-item>
          </el-form>

          <el-alert
            title="修改密码后，你在其它设备上的登录会全部失效，需要重新登录；当前设备会自动续期，不受影响。"
            type="warning"
            :closable="false"
            show-icon
          />
        </el-card>
      </el-col>
    </el-row>

    <!-- 我的问答统计。放在最下面，因为它是"看看就好"的信息，
         不像改密码那样是用户专门进来要做的事 -->
    <el-card shadow="never" class="stats-card">
      <template #header>
        <span class="card-title">我的问答统计</span>
      </template>

      <div v-loading="loadingStats" class="mini-stats">
        <div v-for="item in myStatItems" :key="item.label" class="mini-stat">
          <div class="mini-value">{{ item.value }}</div>
          <div class="mini-label">{{ item.label }}</div>
        </div>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.profile-page {
  height: 100%;
  overflow: auto;
}

.card-title {
  font-weight: 600;
}

.role-tip {
  margin-top: 16px;
}

.stats-card {
  margin-top: 16px;
}

/* 自适应网格：宽屏一行 8 个，窄屏自动折成 4 个、2 个，
   不用为每个断点单独写列数 */
.mini-stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
  gap: 12px;
}

.mini-stat {
  text-align: center;
  padding: 12px 4px;
  background: #fafafa;
  border-radius: 6px;
}

.mini-value {
  font-size: 19px;
  font-weight: 600;
  color: #303133;
}

.mini-label {
  margin-top: 2px;
  font-size: 12px;
  color: #909399;
}
</style>
