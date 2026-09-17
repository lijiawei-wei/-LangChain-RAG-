/**
 * 格式化工具函数。抽出来放公共文件，避免每个页面各写一份。
 */

/** 把字节数转成人看得懂的大小 */
export function formatFileSize(bytes) {
  if (!bytes && bytes !== 0) return '—'
  if (bytes < 1024) return `${bytes} B`

  const units = ['KB', 'MB', 'GB']
  let size = bytes / 1024
  let i = 0
  while (size >= 1024 && i < units.length - 1) {
    size /= 1024
    i += 1
  }
  return `${size.toFixed(1)} ${units[i]}`
}

/** 把时间字符串转成 yyyy-MM-dd HH:mm:ss */
export function formatTime(value) {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString('zh-CN', { hour12: false })
}

/** 相对时间：几分钟前、几小时前。列表里显示"3 分钟前"比完整时间更好扫读 */
export function formatRelativeTime(value) {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return '—'

  const diffSeconds = Math.floor((Date.now() - d.getTime()) / 1000)
  if (diffSeconds < 60) return '刚刚'
  if (diffSeconds < 3600) return `${Math.floor(diffSeconds / 60)} 分钟前`
  if (diffSeconds < 86400) return `${Math.floor(diffSeconds / 3600)} 小时前`
  if (diffSeconds < 86400 * 7) return `${Math.floor(diffSeconds / 86400)} 天前`
  return formatTime(value)
}

/**
 * 文档处理状态 → 显示用的文字、颜色、图标
 *
 * 集中在一处定义：以后加新状态或改文案，只改这里，
 * 不用在表格、筛选器、详情页里到处找。
 */
export const DOCUMENT_STATUS_MAP = {
  pending: { label: '待处理', type: 'info', icon: 'Clock' },
  parsing: { label: '解析中', type: 'warning', icon: 'Loading' },
  chunking: { label: '分块中', type: 'warning', icon: 'Loading' },
  embedding: { label: '向量化中', type: 'warning', icon: 'Loading' },
  ready: { label: '已完成', type: 'success', icon: 'CircleCheck' },
  failed: { label: '失败', type: 'danger', icon: 'CircleClose' },
}

/** 状态筛选下拉框的选项 */
export const DOCUMENT_STATUS_OPTIONS = Object.entries(DOCUMENT_STATUS_MAP).map(
  ([value, config]) => ({ value, label: config.label }),
)

/** 根据状态取显示配置，未知状态给个兜底值 */
export function getStatusConfig(status) {
  return DOCUMENT_STATUS_MAP[status] || { label: status || '未知', type: 'info', icon: 'QuestionFilled' }
}

/**
 * 给数字加千分位分隔符，看板上几十万的 token 数一眼能读出量级。
 * 1234567 → "1,234,567"
 */
export function formatNumber(value) {
  if (value === null || value === undefined) return '—'
  return Number(value).toLocaleString('zh-CN')
}

/**
 * 把 token 数转成好读的形式。
 * 看板上 token 动辄几十万，全写出来会把卡片撑爆，
 * 过万就换算成"万"，保留一位小数。
 */
export function formatTokens(value) {
  const n = Number(value) || 0
  if (n < 10000) return formatNumber(n)
  return `${(n / 10000).toFixed(1)} 万`
}

/**
 * 毫秒转成人能感知的时长。
 *
 * 为什么不用"0.3 秒"这种统一格式：看板上要对比的往往是
 * "首字延迟 300 毫秒"和"总耗时 2.4 秒"，量级差十倍，
 * 都用秒显示的话前者会变成"0.3 秒"，读起来反而绕。
 * 所以小于 1 秒的用毫秒，一眼就知道"很快"。
 */
export function formatDuration(ms) {
  const n = Number(ms) || 0
  if (n === 0) return '—'
  if (n < 1000) return `${n} ms`
  return `${(n / 1000).toFixed(1)} s`
}

/**
 * 描述当前检索范围。
 *
 * **为什么要把知识库名字直接写在界面上**：实测踩过一个坑——新建了「服装」
 * 知识库、资料也传上去了，但顶部「检索范围」还停在「手机数码」，于是问什么
 * 都答不上来，而界面上完全看不出它是在哪个库里找的。用户只会认为"系统坏了"。
 *
 * @param {string} kbName 选中的知识库名字；没选（检索全部）时传空
 * @returns {string} 「服装」或 全部知识库
 */
export function describeScope(kbName) {
  return kbName ? `「${kbName}」` : '全部知识库'
}
