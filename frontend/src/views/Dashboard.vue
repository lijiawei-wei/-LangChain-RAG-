<script setup>
/**
 * 管理员统计看板。
 *
 * 数据全部来自后端实时计算（没有定时汇总表），所以每次进页面看到的都是最新的。
 *
 * 看板的指标是**挑过的**，不是有什么就摆什么：
 *   问答量、token 消耗 —— 说明系统被用得怎么样、花了多少钱
 *   缓存命中率        —— 说明缓存优化有没有真的起作用
 *   平均响应延迟      —— 说明系统快不快（**不含缓存命中**，理由见下）
 *   平均首字延迟      —— 说明用户"感觉"快不快，这是流式输出的价值所在
 *   满意度            —— 说明答得对不对，也是发现知识库盲区的线索
 */
import { ElMessage } from 'element-plus'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

// ECharts 按需引入。
//
// **不要写成 `import * as echarts from 'echarts'`**：那样会把所有图表类型
// 和组件全部打进产物里。实测过两种写法的差别：
//     全量引入   → 1126 KB
//     按需引入   →  542 KB（省掉 584 KB，超过一半）
// 而这里总共只用到折线图和柱状图两种。
// 这个项目在 M3 阶段已经因为 highlight.js 全量引入吃过一次亏
// （聊天页的包从 137KB 涨到 1005KB），同样的坑不踩第二次。
import { BarChart, LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import * as echarts from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'

import {
  getOverview,
  getRecentFeedback,
  getRuntimeStats,
  getTopQuestions,
  getTrend,
} from '@/api/stats'
import { formatDuration, formatNumber, formatRelativeTime, formatTokens } from '@/utils/format'

echarts.use([
  LineChart,
  BarChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  CanvasRenderer,
])

// ---------- 状态 ----------
const loading = ref(false)
const overview = ref(null)
const runtime = ref(null)
const trend = ref([])
const topQuestions = ref([])
const recentDislikes = ref([])
const trendDays = ref(7)

const trendRef = ref(null)
const topRef = ref(null)
let trendChart = null
let topChart = null

/** 顶部指标卡。集中定义，加一项只改这里 */
const statCards = computed(() => {
  const o = overview.value
  if (!o) return []

  return [
    {
      key: 'total',
      label: '问答总数',
      value: formatNumber(o.qa.total),
      sub: `今日 ${formatNumber(o.qa.today)} 次`,
      icon: 'ChatDotRound',
      color: '#409eff',
    },
    {
      key: 'cache',
      label: '缓存命中率',
      // 命中率为 0 时显示 0%，这是真实数据不是"没数据"——
      // 说明缓存还没被用上，管理员应该注意到这一点
      value: `${o.qa.cache_hit_rate}%`,
      sub: `命中 ${formatNumber(o.qa.cache_hits)} 次`,
      icon: 'Lightning',
      color: '#67c23a',
    },
    {
      key: 'latency',
      label: '平均响应延迟',
      value: formatDuration(o.qa.avg_latency_ms),
      sub: '不含缓存命中',
      icon: 'Timer',
      color: '#e6a23c',
    },
    {
      key: 'ttft',
      label: '平均首字延迟',
      value: formatDuration(o.qa.avg_first_token_ms),
      sub: '提问到看见第一个字',
      icon: 'Odometer',
      color: '#909399',
    },
    {
      key: 'tokens',
      label: 'Token 消耗',
      value: formatTokens(o.qa.total_tokens),
      sub: `输入 ${formatTokens(o.qa.prompt_tokens)} / 输出 ${formatTokens(o.qa.completion_tokens)}`,
      icon: 'Coin',
      color: '#f56c6c',
    },
    {
      key: 'satisfaction',
      label: '回答满意度',
      // 一条反馈都没有时显示"—"而不是"0%"。
      // 0% 会被读成"用户全都不满意"，而实际上只是还没人评价
      value: o.feedback.likes + o.feedback.dislikes === 0
        ? '—'
        : `${Math.round(o.feedback.satisfaction * 100)}%`,
      sub: `赞 ${o.feedback.likes} / 踩 ${o.feedback.dislikes}`,
      icon: 'Star',
      color: '#409eff',
    },
  ]
})

// ---------- 加载 ----------
async function loadAll() {
  loading.value = true
  try {
    // 五个请求互不依赖，用 allSettled 并行发。
    // 用 allSettled 而不是 all：某一块数据拉失败（比如统计查询出错）时，
    // 其他部分还能正常显示，不至于整页空白
    const [ov, rt, tr, tq, fb] = await Promise.allSettled([
      getOverview(),
      getRuntimeStats(),
      getTrend(trendDays.value),
      getTopQuestions(10),
      getRecentFeedback(-1, 8),
    ])

    if (ov.status === 'fulfilled') overview.value = ov.value
    if (rt.status === 'fulfilled') runtime.value = rt.value
    if (tr.status === 'fulfilled') trend.value = tr.value
    if (tq.status === 'fulfilled') topQuestions.value = tq.value
    if (fb.status === 'fulfilled') recentDislikes.value = fb.value
  } finally {
    loading.value = false
  }
}

/** 只重新拉趋势数据（切换天数时用，没必要把整页都刷一遍） */
async function reloadTrend() {
  try {
    trend.value = await getTrend(trendDays.value)
  } catch {
    // 错误提示由拦截器统一处理
  }
}

onMounted(async () => {
  // 先初始化一次图表（此时还没数据，画出来是空的，但实例建好了）
  await initCharts()
  await loadAll()
  window.addEventListener('resize', resizeCharts)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', resizeCharts)
  // **必须销毁**。ECharts 实例持有 canvas 和事件监听，
  // 不销毁的话反复进出这个页面会持续泄漏内存
  trendChart?.dispose()
  topChart?.dispose()
  trendChart = null
  topChart = null
})

function resizeCharts() {
  trendChart?.resize()
  topChart?.resize()
}

// ---------- 图表 ----------
/**
 * 同步图表：该建的建、该销毁的销毁、该重画的画。
 *
 * **为什么不能只在 onMounted 里建一次**：「高频问题」的容器是 v-if 控制的
 * （没有数据时显示的是"还没有问答记录"这句话），首次进页面如果一条问答都还没有，
 * 那个 div 根本不存在，图表实例也就建不出来。
 * 之后有数据了，容器才出现——如果这里不去补建实例，
 * 图表区域就会一直空着，怎么点刷新都没用，只能刷新整个页面。
 *
 * 反过来，数据变空时（比如清空了问答记录）容器被移除，
 * 这时**必须把实例销毁掉**，否则它握着已经不在页面上的 canvas 不放，内存泄漏。
 */
async function initCharts() {
  // 等 DOM 更新完再操作。容器的出现/消失是由 v-if 决定的，
  // 时机上比数据晚一步
  await nextTick()

  if (trendRef.value) {
    if (!trendChart) trendChart = echarts.init(trendRef.value)
    trendChart.setOption(buildTrendOption(trend.value), true)
  } else if (trendChart) {
    trendChart.dispose()
    trendChart = null
  }

  if (topRef.value) {
    if (!topChart) topChart = echarts.init(topRef.value)
    topChart.setOption(buildTopOption(topQuestions.value), true)
  } else if (topChart) {
    topChart.dispose()
    topChart = null
  }
}

// 数据变化 → 重画。用 flush: 'post' 让回调在 DOM 更新之后执行，
// 这样 initCharts 里能拿到刚渲染出来的容器
watch([trend, topQuestions], initCharts, { flush: 'post' })

function buildTrendOption(items) {
  const dates = items.map((i) => i.date.slice(5)) // 只显示 MM-DD，年份挤在轴上看不清

  return {
    tooltip: { trigger: 'axis' },
    legend: { data: ['问答量', 'Token 消耗'], right: 0, top: 0, icon: 'roundRect' },
    grid: { left: 8, right: 8, top: 40, bottom: 8, containLabel: true },
    xAxis: {
      type: 'category',
      data: dates,
      boundaryGap: false,
      axisLine: { lineStyle: { color: '#e4e7ed' } },
      axisLabel: { color: '#909399' },
    },
    // 双 Y 轴：问答量是个位数，token 是六位数，共用一个轴的话
    // 问答量的折线会被压成贴着底边的一条直线，什么都看不出来
    yAxis: [
      {
        type: 'value',
        name: '次数',
        nameTextStyle: { color: '#909399' },
        axisLabel: { color: '#909399' },
        splitLine: { lineStyle: { color: '#f5f7fa' } },
      },
      {
        type: 'value',
        name: 'Token',
        nameTextStyle: { color: '#909399' },
        axisLabel: { color: '#909399' },
        splitLine: { show: false },
      },
    ],
    series: [
      {
        name: '问答量',
        type: 'line',
        smooth: true,
        data: items.map((i) => i.questions),
        itemStyle: { color: '#409eff' },
        areaStyle: { color: 'rgba(64,158,255,0.12)' },
      },
      {
        name: 'Token 消耗',
        type: 'line',
        smooth: true,
        yAxisIndex: 1,
        data: items.map((i) => i.tokens),
        itemStyle: { color: '#e6a23c' },
      },
    ],
  }
}

function buildTopOption(items) {
  // 横向柱状图按次数升序排，最多的显示在最上面
  const sorted = [...items].sort((a, b) => a.times - b.times)

  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 8, right: 30, top: 10, bottom: 8, containLabel: true },
    xAxis: {
      type: 'value',
      axisLabel: { color: '#909399' },
      splitLine: { lineStyle: { color: '#f5f7fa' } },
    },
    yAxis: {
      type: 'category',
      // 问题太长会把坐标轴撑得很宽，截断一下，完整内容放在悬浮提示里
      data: sorted.map((i) => (i.question.length > 12 ? `${i.question.slice(0, 12)}…` : i.question)),
      axisLabel: { color: '#606266' },
      axisLine: { lineStyle: { color: '#e4e7ed' } },
    },
    series: [
      {
        type: 'bar',
        data: sorted.map((i) => i.times),
        barWidth: '55%',
        itemStyle: { color: '#409eff', borderRadius: [0, 4, 4, 0] },
        label: { show: true, position: 'right', color: '#909399' },
      },
    ],
  }
}
</script>

<template>
  <div v-loading="loading" class="dashboard-page">
    <!-- 顶部：标题 + 操作 -->
    <div class="page-head">
      <div>
        <h2 class="page-title">统计看板</h2>
        <span class="page-sub">数据实时统计，刷新即最新</span>
      </div>
      <el-button :icon="'Refresh'" @click="loadAll">刷新</el-button>
    </div>

    <!-- 指标卡 -->
    <div class="stat-grid">
      <el-card v-for="card in statCards" :key="card.key" shadow="never" class="stat-card">
        <div class="stat-body">
          <div class="stat-icon" :style="{ background: `${card.color}1a`, color: card.color }">
            <el-icon :size="20"><component :is="card.icon" /></el-icon>
          </div>
          <div class="stat-text">
            <div class="stat-label">{{ card.label }}</div>
            <div class="stat-value">{{ card.value }}</div>
            <div class="stat-sub">{{ card.sub }}</div>
          </div>
        </div>
      </el-card>
    </div>

    <!-- 趋势图 -->
    <el-card shadow="never" class="block">
      <template #header>
        <div class="block-header">
          <span class="block-title">问答趋势</span>
          <el-radio-group v-model="trendDays" size="small" @change="reloadTrend">
            <el-radio-button :value="7">近 7 天</el-radio-button>
            <el-radio-button :value="30">近 30 天</el-radio-button>
          </el-radio-group>
        </div>
      </template>
      <div ref="trendRef" class="chart chart-trend" />
    </el-card>

    <!-- 高频问题 + 最近被点踩 -->
    <el-row :gutter="12">
      <el-col :xs="24" :md="12">
        <el-card shadow="never" class="block">
          <template #header>
            <span class="block-title">高频问题</span>
          </template>
          <div v-if="topQuestions.length === 0" class="empty-hint">还没有问答记录</div>
          <div v-else ref="topRef" class="chart chart-top" />
        </el-card>
      </el-col>

      <el-col :xs="24" :md="12">
        <el-card shadow="never" class="block">
          <template #header>
            <div class="block-header">
              <span class="block-title">最近被点踩的回答</span>
              <!-- 这块的价值不在"统计"，而在"排查"：
                   管理员看到哪类问题集中被踩，就知道该补哪方面的资料 -->
              <el-tag size="small" type="info">知识库盲区线索</el-tag>
            </div>
          </template>

          <div v-if="recentDislikes.length === 0" class="empty-hint">
            还没有被点踩的回答
          </div>
          <div v-else class="dislike-list">
            <div v-for="item in recentDislikes" :key="item.id" class="dislike-item">
              <div class="dislike-q">
                <el-icon class="dislike-mark"><CloseBold /></el-icon>
                {{ item.question || '（没有记录到原问题）' }}
              </div>
              <div class="dislike-a">{{ item.answer_excerpt }}…</div>
              <div v-if="item.comment" class="dislike-comment">用户说：{{ item.comment }}</div>
              <div class="dislike-meta">
                <span>{{ item.username || '已注销用户' }}</span>
                <span>{{ formatRelativeTime(item.created_at) }}</span>
              </div>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 系统规模 + 运行时状态 -->
    <el-row :gutter="12">
      <el-col :xs="24" :md="12">
        <el-card shadow="never" class="block">
          <template #header>
            <span class="block-title">系统规模</span>
          </template>
          <el-descriptions v-if="overview" :column="2" border size="small">
            <el-descriptions-item label="用户数">
              {{ overview.users.total }}（启用 {{ overview.users.active }}）
            </el-descriptions-item>
            <el-descriptions-item label="知识库">{{ overview.knowledge_bases }} 个</el-descriptions-item>
            <el-descriptions-item label="文档数">
              {{ overview.documents.total }}（成功 {{ overview.documents.ready }}）
            </el-descriptions-item>
            <el-descriptions-item label="分块总数">{{ formatNumber(overview.chunks) }}</el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-col>

      <el-col :xs="24" :md="12">
        <el-card shadow="never" class="block">
          <template #header>
            <div class="block-header">
              <span class="block-title">运行时状态</span>
              <!-- 说清楚这些数字的口径，避免被误读成"累计值" -->
              <span class="block-hint">服务重启后归零</span>
            </div>
          </template>
          <el-descriptions v-if="runtime" :column="2" border size="small">
            <el-descriptions-item label="缓存条目">
              {{ runtime.cache_entries }} / {{ runtime.cache_maxsize }}
            </el-descriptions-item>
            <el-descriptions-item label="缓存淘汰">{{ runtime.cache_evictions }}</el-descriptions-item>
            <el-descriptions-item label="正在生成">{{ runtime.active_concurrency }} 个</el-descriptions-item>
            <el-descriptions-item label="失败文档">
              {{ overview?.documents.failed ?? 0 }} 个
            </el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.dashboard-page {
  padding: 16px;
  overflow-y: auto;
  height: 100%;
}

.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 14px;
}

.page-title {
  margin: 0;
  font-size: 18px;
  color: #303133;
}

.page-sub {
  font-size: 12px;
  color: #909399;
}

/* ---------- 指标卡 ---------- */
/* 用 auto-fit 而不是固定列数：屏幕窄的时候自动折行，
   不用为每个断点单独写一遍列数 */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 12px;
  margin-bottom: 12px;
}

.stat-card :deep(.el-card__body) {
  padding: 14px;
}

.stat-body {
  display: flex;
  align-items: center;
  gap: 12px;
}

.stat-icon {
  width: 40px;
  height: 40px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.stat-text {
  min-width: 0;
}

.stat-label {
  font-size: 12px;
  color: #909399;
}

.stat-value {
  font-size: 22px;
  font-weight: 600;
  color: #303133;
  line-height: 1.3;
}

.stat-sub {
  font-size: 11px;
  color: #c0c4cc;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---------- 通用块 ---------- */
.block {
  margin-bottom: 12px;
}

.block :deep(.el-card__header) {
  padding: 12px 14px;
}

.block-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.block-title {
  font-weight: 600;
  font-size: 14px;
  color: #303133;
}

.block-hint {
  font-size: 11px;
  color: #c0c4cc;
}

.chart {
  width: 100%;
}

.chart-trend {
  height: 260px;
}

.chart-top {
  height: 260px;
}

.empty-hint {
  padding: 40px 0;
  text-align: center;
  color: #c0c4cc;
  font-size: 13px;
}

/* ---------- 被点踩列表 ---------- */
.dislike-list {
  max-height: 260px;
  overflow-y: auto;
}

.dislike-item {
  padding: 10px 0;
  border-bottom: 1px dashed #f0f0f0;
}

.dislike-item:last-child {
  border-bottom: none;
}

.dislike-q {
  font-size: 13px;
  color: #303133;
  margin-bottom: 4px;
  display: flex;
  align-items: center;
  gap: 4px;
}

.dislike-mark {
  color: #f56c6c;
  flex-shrink: 0;
}

.dislike-a {
  font-size: 12px;
  color: #909399;
  line-height: 1.5;
  /* 回答正文截断成两行，列表里不需要看全文 */
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.dislike-comment {
  margin-top: 4px;
  font-size: 12px;
  color: #e6a23c;
  background: #fdf6ec;
  padding: 3px 8px;
  border-radius: 4px;
  display: inline-block;
}

.dislike-meta {
  margin-top: 4px;
  font-size: 11px;
  color: #c0c4cc;
  display: flex;
  gap: 12px;
}
</style>
