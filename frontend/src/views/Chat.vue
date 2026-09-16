<script setup>
/**
 * 知识库问答主界面。
 *
 * 三栏布局：
 *   左 —— 会话列表（新建 / 重命名 / 删除）
 *   中 —— 消息流（Markdown 渲染、引用角标可点击）
 *   右 —— 引用来源面板（点 [n] 角标时高亮对应的原文片段）
 *
 * 当前是非流式版本（回答一次性返回）。流式打字机效果会在 M4 加上，
 * 届时只需要把发送逻辑换成 SSE 消费，其余部分不用动。
 */
import { Search } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'

import {
  clearConversations,
  deleteConversation,
  listConversations,
  listMessages,
  updateConversation,
} from '@/api/conversation'
import { cancelFeedback, submitFeedback } from '@/api/feedback'
import { listAllKnowledgeBases } from '@/api/kb'
import CitationPanel from '@/components/CitationPanel.vue'
import MarkdownRenderer from '@/components/MarkdownRenderer.vue'
import { useUserStore } from '@/stores/user'
import { formatRelativeTime } from '@/utils/format'
import { streamChat } from '@/utils/sse'

const userStore = useUserStore()

// ---------- 状态 ----------
const conversations = ref([])
const currentConversation = ref(null)
const messages = ref([])

const knowledgeBases = ref([])
const selectedKbId = ref(null)

const input = ref('')
const sending = ref(false)
const loadingMessages = ref(false)
const loadingConversations = ref(false)

// 会话搜索关键字。搜索是**服务端做的**（后端会话列表接口支持 keyword 参数），
// 不是前端过滤——这样以后会话多了、列表分页了也能搜到。
// 但要做防抖，否则每敲一个字就发一次请求，用户打字快的时候会连发十几个
const convKeyword = ref('')
let searchTimer = null

/** 输入搜索词后延迟 300 毫秒再查，避免每敲一个字发一次请求 */
function handleSearchInput() {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    loadConversations(currentConversation.value?.id ?? null, { keepSelection: true })
  }, 300)
}

// 流式生成相关
const streamingMessageId = ref(null) // 正在逐字输出的那条消息
const abortController = ref(null) // 用于中断流式请求

/** 是否正在流式输出（决定要不要显示"停止生成"按钮） */
const isStreaming = computed(() => streamingMessageId.value !== null)

// 右侧引用面板
const panelCitations = ref([])
const highlightIndex = ref(null)
const panelMessageId = ref(null)

let messageListRef = null

/** 输入框下方的提示文字 */
const canSend = computed(() => input.value.trim().length > 0 && !sending.value)

// ---------- 初始化 ----------
onMounted(async () => {
  await Promise.all([loadKnowledgeBases(), loadConversations()])
})

async function loadKnowledgeBases() {
  try {
    knowledgeBases.value = await listAllKnowledgeBases()
  } catch {
    // 拉不到知识库列表不影响聊天，静默处理
  }
}

/**
 * 拉取会话列表。
 *
 * @param {number|null} selectId 拉完后要选中哪个会话
 * @param {object} options
 *   keepSelection —— 只刷新列表，不改变当前选中的会话。
 *     搜索时必须传 true：用户在搜索框里打字只是想找某个会话，
 *     如果每次都自动跳到列表第一条，他正在看的对话就被切走了。
 */
async function loadConversations(selectId = null, { keepSelection = false } = {}) {
  loadingConversations.value = true
  try {
    const data = await listConversations({
      pageSize: 100,
      keyword: convKeyword.value.trim() || undefined,
    })
    conversations.value = data.items

    if (keepSelection) return

    const target =
      conversations.value.find((c) => c.id === selectId) ||
      conversations.value.find((c) => c.id === currentConversation.value?.id) ||
      conversations.value[0] ||
      null

    if (target) {
      await selectConversation(target)
    } else {
      currentConversation.value = null
      messages.value = []
    }
  } finally {
    loadingConversations.value = false
  }
}

async function selectConversation(conversation) {
  currentConversation.value = conversation
  selectedKbId.value = conversation.kb_id ?? null
  panelCitations.value = []
  highlightIndex.value = null
  panelMessageId.value = null

  loadingMessages.value = true
  try {
    messages.value = await listMessages(conversation.id, { limit: 100 })
    await scrollToBottom()
  } finally {
    loadingMessages.value = false
  }
}

// ---------- 会话操作 ----------
function handleNewConversation() {
  // 不立刻调接口建会话，而是等用户发出第一条消息时再建。
  // 好处：用户点了「新对话」又没说话就走，不会在列表里留下一堆空会话
  currentConversation.value = null
  messages.value = []
  input.value = ''
  panelCitations.value = []
  highlightIndex.value = null
  // 清掉搜索词。否则用户搜着"手机"的时候点了新对话，
  // 聊完一轮发现左边列表里找不到它——因为新会话不匹配"手机"这个关键字
  convKeyword.value = ''
}

async function handleRename(conversation) {
  try {
    const { value } = await ElMessageBox.prompt('请输入新的会话名称', '重命名', {
      confirmButtonText: '保存',
      cancelButtonText: '取消',
      inputValue: conversation.title,
      inputPattern: /\S/,
      inputErrorMessage: '名称不能为空',
    })
    await updateConversation(conversation.id, { title: value.trim() })
    ElMessage.success('已重命名')
    await loadConversations(conversation.id)
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') {
      // 真实错误已由拦截器提示
    }
  }
}

async function handleDelete(conversation) {
  try {
    await ElMessageBox.confirm(
      `确定删除会话「${conversation.title}」吗？里面的对话记录会一并清除。`,
      '删除会话',
      { confirmButtonText: '确定删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }

  await deleteConversation(conversation.id)
  ElMessage.success('会话已删除')

  if (currentConversation.value?.id === conversation.id) {
    handleNewConversation()
  }
  await loadConversations()
}

async function handleClearAll() {
  try {
    await ElMessageBox.confirm(
      '确定清空所有会话吗？全部对话记录都会被删除，且无法恢复。',
      '清空会话',
      { confirmButtonText: '确定清空', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }

  await clearConversations()
  ElMessage.success('已清空全部会话')
  handleNewConversation()
  await loadConversations()
}

// ---------- 发送提问（流式）----------
async function handleSend() {
  if (!canSend.value) return

  const question = input.value.trim()
  input.value = ''
  sending.value = true

  // 用户的问题先立刻显示出来（乐观更新），界面马上有反应
  const tempUserId = `temp-user-${Date.now()}`
  messages.value.push({
    id: tempUserId,
    role: 'user',
    content: question,
    citations: [],
    created_at: new Date().toISOString(),
  })

  // 同时创建一个空的助手消息占位，等第一个 token 到达时往里填字。
  // 先占位再填，用户看到的是"气泡已经在打字了"，
  // 而不是"先等一会儿，然后突然冒出一个气泡"
  const assistantId = `temp-assistant-${Date.now()}`
  messages.value.push({
    id: assistantId,
    role: 'assistant',
    content: '',
    citations: [],
    created_at: new Date().toISOString(),
    streaming: true,
    // 新回答当然还没被评价过。显式写出来是为了让按钮的选中状态有个明确初值，
    // 不然模板里读到 undefined，判断逻辑会多一层兼容分支
    my_feedback: null,
  })
  streamingMessageId.value = assistantId

  await scrollToBottom()

  const findMessage = (id) => messages.value.find((m) => m.id === id)

  abortController.value = new AbortController()

  await streamChat({
    question,
    conversationId: currentConversation.value?.id ?? null,
    kbId: selectedKbId.value,
    token: userStore.accessToken,
    refreshToken: () => userStore.refreshTokenAction(),
    signal: abortController.value.signal,

    handlers: {
      // 会话信息到达：把临时消息换成服务端返回的真实信息
      onMeta(data) {
        const userMsg = findMessage(tempUserId)
        if (userMsg) userMsg.id = data.user_message_id

        if (!currentConversation.value) {
          // 新会话：先用服务端返回的信息占个位，
          // 完整的会话对象等 done 之后再去刷新列表拿
          currentConversation.value = {
            id: data.conversation_id,
            title: data.title || question,
            message_count: 2,
            updated_at: new Date().toISOString(),
          }
        }
      },

      // 引用来源先于文字到达，可以立刻渲染右侧面板
      onSources(data) {
        const msg = findMessage(assistantId)
        if (msg) {
          msg.citations = data.citations || []
        }
        if (data.citations?.length > 0) {
          panelMessageId.value = assistantId
          panelCitations.value = data.citations
        }
      },

      // 逐字追加
      onToken(delta) {
        const msg = findMessage(assistantId)
        if (msg) {
          msg.content += delta
          // 边打字边滚到底，但有条件——见 maybeAutoScroll 的说明
          maybeAutoScroll()
        }
      },

      // 生成结束：补上统计信息和最终的引用标记
      onDone(data) {
        const msg = findMessage(assistantId)
        if (msg) {
          msg.id = data.assistant_message_id
          msg.content = data.answer || msg.content
          msg.citations = data.citations || msg.citations
          msg.latency_ms = data.latency_ms
          msg.prompt_tokens = data.prompt_tokens
          msg.completion_tokens = data.completion_tokens
          msg.streaming = false

          if (msg.citations.length > 0) {
            panelMessageId.value = msg.id
            panelCitations.value = msg.citations
          }
        }
        finishStreaming()
        refreshConversationList(currentConversation.value?.id)
      },

      onError(err) {
        const msg = findMessage(assistantId)
        if (msg) {
          msg.streaming = false
          // 已经吐出一部分文字的话要保留，不能因为最后出错就把回答清空
          msg.error = err.message || '生成回答失败'
          if (!msg.content) {
            msg.content = ''
          }
        }
        finishStreaming()

        // 登录过期由 sse.js 负责提示跳转，这里不重复弹窗
        if (!err.expired) {
          ElMessage.error(err.message || '生成回答失败')
        }
      },
    },
  })
}

function finishStreaming() {
  sending.value = false
  streamingMessageId.value = null
  abortController.value = null
}

/** 用户点「停止生成」 */
function handleStop() {
  abortController.value?.abort()
  const msg = messages.value.find((m) => m.id === streamingMessageId.value)
  if (msg) {
    msg.streaming = false
    // 已经生成的部分保留下来，用户至少能看到半截回答；
    // 并标上"已中断"，避免他以为这就是完整答案
    if (!msg.content) {
      msg.content = '（已停止生成）'
    } else {
      msg.interrupted = true
    }
  }
  ElMessage.info('已停止生成')
  finishStreaming()
}

/**
 * 智能滚动到底部。
 *
 * 规则：只有当用户当前就在底部附近（差 120 像素以内）才自动滚。
 * 为什么这么做：用户可能在生成过程中往上翻看前面的内容，
 * 如果我们每来一个字就硬滚到底，他根本没法读——滚动条会被一直拽走。
 */
function maybeAutoScroll() {
  const el = document.querySelector('.message-scroll')
  if (!el) return

  const distanceToBottom = el.scrollHeight - el.scrollTop - el.clientHeight
  if (distanceToBottom < 120) {
    el.scrollTop = el.scrollHeight
  }
}

async function refreshConversationList(selectId) {
  // 这里**不带搜索关键字**：它的用途是"刷新标题和排序"，
  // 不是响应用户搜索。带上关键字会导致新会话一旦不匹配当前搜索词，
  // 就从列表里消失，看起来像没创建成功
  const data = await listConversations({ pageSize: 100 })
  conversations.value = data.items
  const target = conversations.value.find((c) => c.id === selectId)
  if (target) {
    currentConversation.value = target
  }
}

// ---------- 答案反馈（点赞/点踩）----------
/**
 * 给回答点赞或点踩。
 *
 * 交互规则是「再点一次就取消」，所以要先看当前状态：
 *   - 点的是**已经选中**的那个按钮 → 取消评价
 *   - 点的是另一个按钮（赞改踩）→ 直接提交，后端会覆盖掉旧的那条
 */
async function handleFeedback(message, rating) {
  // 消息 ID 必须是后端返回的真实数字。
  // 生成被中断或出错时，消息还挂着前端造的临时字符串 ID（temp-assistant-xxx），
  // 拿它去请求接口会拿到 422，用户看到的就是莫名其妙的"参数校验失败"
  if (typeof message.id !== 'number') {
    ElMessage.warning('这条回答没有保存成功，无法评价')
    return
  }

  const previous = message.my_feedback

  // 先乐观更新界面：点下去立刻有反应，不用等网络往返。
  // 万一请求失败，catch 里会改回去
  message.my_feedback = previous === rating ? null : rating

  try {
    if (previous === rating) {
      await cancelFeedback(message.id)
    } else {
      await submitFeedback(message.id, rating)
    }
  } catch {
    // 请求失败（网络问题、回答被删了等），把界面改回原样，
    // 不能让用户以为评价成功了——那会污染后面看板上的统计
    message.my_feedback = previous
  }
}

// ---------- 引用面板 ----------
function showCitations(message) {
  panelMessageId.value = message.id
  panelCitations.value = message.citations || []
}

function handleCitationClick(index) {
  // 找到当前这条消息对应的引用（点击角标时已经在面板里了）
  highlightIndex.value = index
}

/** 点击消息里的角标 */
function handleBadgeClick(message, index) {
  showCitations(message)
  // 用 nextTick 让面板先渲染出来再设高亮，否则滚动定位找不到元素
  nextTick(() => {
    highlightIndex.value = index
  })
}

// ---------- 滚动 ----------
async function scrollToBottom() {
  await nextTick()
  const el = document.querySelector('.message-scroll')
  if (el) {
    el.scrollTop = el.scrollHeight
  }
}

// ---------- 输入框 ----------
function handleKeydown(event) {
  // Enter 发送，Shift+Enter 换行 —— 和主流聊天软件一致
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    handleSend()
  }
}

// ---------- 其它 ----------
/** 切换知识库时，如果是已有会话就提示用户，避免以为改了立刻生效 */
function handleKbChange(value) {
  if (currentConversation.value) {
    ElMessage.info('知识库已切换，下一条提问将按新选择检索')
  }
}

onUnmounted(() => {
  // 搜索的防抖定时器必须清掉：不然用户搜到一半切到别的页面，
  // 300 毫秒后回调还会执行，去操作一个已经卸载的组件
  clearTimeout(searchTimer)

  // 离开页面时中断正在进行的流式请求，别让它在后台白跑
  abortController.value?.abort()
})
</script>

<template>
  <div class="chat-page">
    <!-- 左：会话列表 -->
    <el-card class="conv-panel" shadow="never" body-style="padding:0">
      <template #header>
        <div class="panel-header">
          <span class="panel-title">会话</span>
          <el-button type="primary" size="small" @click="handleNewConversation">
            <el-icon><Plus /></el-icon>新对话
          </el-button>
        </div>
      </template>

      <div class="conv-search">
        <el-input
          v-model="convKeyword"
          placeholder="搜索会话"
          size="small"
          clearable
          :prefix-icon="Search"
          @input="handleSearchInput"
          @clear="handleSearchInput"
        />
      </div>

      <div v-loading="loadingConversations" class="conv-list">
        <el-empty
          v-if="conversations.length === 0 && !loadingConversations"
          :description="convKeyword.trim() ? '没有匹配的会话' : '还没有对话'"
          :image-size="70"
        />

        <div
          v-for="conv in conversations"
          :key="conv.id"
          class="conv-item"
          :class="{ active: currentConversation?.id === conv.id }"
          @click="selectConversation(conv)"
        >
          <div class="conv-title" :title="conv.title">{{ conv.title }}</div>
          <div class="conv-meta">
            <span>{{ conv.message_count }} 条</span>
            <span>{{ formatRelativeTime(conv.updated_at) }}</span>
            <span class="conv-actions">
              <el-icon title="重命名" @click.stop="handleRename(conv)"><EditPen /></el-icon>
              <el-icon title="删除" @click.stop="handleDelete(conv)"><Delete /></el-icon>
            </span>
          </div>
        </div>
      </div>

      <div v-if="conversations.length > 0" class="conv-footer">
        <el-button link type="danger" size="small" @click="handleClearAll">
          清空全部会话
        </el-button>
      </div>
    </el-card>

    <!-- 中：消息区 -->
    <el-card class="main-panel" shadow="never" body-style="padding:0">
      <template #header>
        <div class="panel-header">
          <div class="panel-title">
            {{ currentConversation?.title || '新对话' }}
          </div>
          <div class="kb-selector">
            <span class="kb-label">检索范围：</span>
            <el-select
              v-model="selectedKbId"
              placeholder="全部知识库"
              clearable
              size="small"
              style="width: 180px"
              @change="handleKbChange"
            >
              <el-option
                v-for="kb in knowledgeBases"
                :key="kb.id"
                :label="`${kb.name}（${kb.chunk_count} 块）`"
                :value="kb.id"
              />
            </el-select>
          </div>
        </div>
      </template>

      <!-- 消息流 -->
      <div class="message-scroll">
        <div v-if="messages.length === 0" class="welcome">
          <el-icon class="welcome-icon"><ChatDotRound /></el-icon>
          <h3>你好，我是商品知识库助手</h3>
          <p>问我任何关于商品的问题，我会查阅知识库里的资料回答，并告诉你答案出自哪里。</p>
          <div class="examples">
            <div class="examples-title">可以试试这样问：</div>
            <el-tag
              v-for="q in [
                '星耀 X9 Pro 的电池容量是多少？',
                '这款手机支持快充吗？充多久能满？',
                '保修期是多久？',
                '包装里都有什么配件？',
              ]"
              :key="q"
              class="example-tag"
              @click="input = q"
            >
              {{ q }}
            </el-tag>
          </div>
        </div>

        <div
          v-for="message in messages"
          :key="message.id"
          class="message-row"
          :class="message.role"
        >
          <div class="avatar">
            <el-icon v-if="message.role === 'user'"><User /></el-icon>
            <el-icon v-else><Service /></el-icon>
          </div>

          <div class="bubble-wrap">
            <!-- 用户消息按纯文本展示，避免用户输入的内容被当成 Markdown 渲染 -->
            <div v-if="message.role === 'user'" class="bubble user-bubble">
              {{ message.content }}
            </div>

            <div v-else class="bubble assistant-bubble" :class="{ failed: message.error }">
              <!-- 正在检索但还没吐字：给个"正在查阅资料"的提示，避免空白气泡 -->
              <div v-if="message.streaming && !message.content" class="thinking">
                <el-icon class="is-loading"><Loading /></el-icon>
                正在查阅资料…
              </div>

              <template v-else>
                <MarkdownRenderer
                  :content="message.content"
                  :citation-count="message.citations?.length || 0"
                  @citation-click="(i) => handleBadgeClick(message, i)"
                />
                <!-- 打字光标：只在流式输出中显示 -->
                <span v-if="message.streaming" class="typing-cursor" />
              </template>

              <!-- 被用户中断 -->
              <el-tag v-if="message.interrupted" size="small" type="info" class="marker">
                已中断，以上为部分回答
              </el-tag>

              <!-- 生成出错 -->
              <el-alert
                v-if="message.error"
                :title="message.error"
                type="error"
                :closable="false"
                show-icon
                class="marker"
              />

              <!-- 底部工具条：引用数量、评价、耗时 -->
              <div v-if="!message.streaming" class="bubble-footer">
                <el-button
                  v-if="message.citations?.length > 0"
                  link
                  type="primary"
                  size="small"
                  @click="showCitations(message)"
                >
                  <el-icon><Document /></el-icon>
                  查看引用来源（{{ message.citations.filter((c) => c.used).length }} 条被引用）
                </el-button>

                <!--
                  点赞/点踩。两个按钮都只在满足下面条件时出现：
                    - 是助手的回答（评价自己的提问没有意义）
                    - 生成过程没有出错
                    - 消息 ID 是后端返回的真实数字。
                      被中断或出错的消息还挂着前端造的临时 ID（temp-assistant-xxx），
                      拿它请求接口只会得到 422，不如直接不给按钮
                -->
                <span
                  v-if="
                    message.role === 'assistant' &&
                    !message.error &&
                    typeof message.id === 'number'
                  "
                  class="feedback-actions"
                >
                  <el-tooltip content="回答有用" placement="top" :show-after="400">
                    <el-icon
                      class="feedback-icon"
                      :class="{ active: message.my_feedback === 1 }"
                      @click="handleFeedback(message, 1)"
                    >
                      <Select />
                    </el-icon>
                  </el-tooltip>
                  <el-tooltip content="回答没用" placement="top" :show-after="400">
                    <el-icon
                      class="feedback-icon dislike"
                      :class="{ active: message.my_feedback === -1 }"
                      @click="handleFeedback(message, -1)"
                    >
                      <CloseBold />
                    </el-icon>
                  </el-tooltip>
                </span>

                <span v-if="message.latency_ms" class="latency">
                  耗时 {{ (message.latency_ms / 1000).toFixed(1) }}s
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 输入区 -->
      <div class="input-area">
        <el-input
          v-model="input"
          type="textarea"
          :rows="3"
          resize="none"
          placeholder="输入你的问题，Enter 发送，Shift + Enter 换行"
          :disabled="sending"
          @keydown="handleKeydown"
        />
        <div class="input-footer">
          <span class="hint">
            {{ selectedKbId ? '正在指定知识库内检索' : '正在全部知识库中检索' }}
          </span>
          <div class="actions">
            <el-button v-if="isStreaming" type="danger" plain @click="handleStop">
              <el-icon><VideoPause /></el-icon>停止生成
            </el-button>
            <el-button
              v-else
              type="primary"
              :disabled="!canSend"
              @click="handleSend"
            >
              <el-icon><Promotion /></el-icon>发送
            </el-button>
          </div>
        </div>
      </div>
    </el-card>

    <!-- 右：引用来源 -->
    <el-card class="cite-panel" shadow="never" body-style="padding:12px">
      <template #header>
        <div class="panel-header">
          <span class="panel-title">引用来源</span>
          <el-tag v-if="panelCitations.length > 0" size="small" type="info">
            {{ panelCitations.length }} 条
          </el-tag>
        </div>
      </template>

      <CitationPanel :citations="panelCitations" :highlight-index="highlightIndex" />
    </el-card>
  </div>
</template>

<style scoped>
.chat-page {
  display: flex;
  gap: 12px;
  height: 100%;
}

/* ---------- 左侧会话列表 ---------- */
.conv-panel {
  flex: 0 0 230px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.conv-panel :deep(.el-card__body) {
  flex: 1;
  overflow-y: auto;
  padding: 6px;
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.panel-title {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 搜索框。用 sticky 吸在顶部，不跟着列表一起滚——
   会话多了往下翻的时候，还能随时改搜索词，不用滚回顶部。
   背景色必须显式给，否则底下的会话会透上来 */
.conv-search {
  position: sticky;
  top: 0;
  z-index: 2;
  padding: 0 2px 6px;
  margin-bottom: 4px;
  background: #fff;
  border-bottom: 1px solid #f0f2f5;
}

.conv-list {
  min-height: 100px;
}

.conv-item {
  padding: 9px 10px;
  border-radius: 6px;
  cursor: pointer;
  margin-bottom: 4px;
  transition: background-color 0.15s;
}

.conv-item:hover {
  background: #f5f7fa;
}

.conv-item.active {
  background: #ecf5ff;
}

.conv-title {
  font-size: 13px;
  color: #303133;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-bottom: 4px;
}

.conv-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: #909399;
}

.conv-actions {
  margin-left: auto;
  display: flex;
  gap: 6px;
  opacity: 0;
  transition: opacity 0.15s;
}

.conv-item:hover .conv-actions {
  opacity: 1;
}

.conv-actions .el-icon:hover {
  color: #409eff;
}

.conv-footer {
  padding: 8px;
  text-align: center;
  border-top: 1px solid #f0f0f0;
}

/* ---------- 中间消息区 ---------- */
.main-panel {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.main-panel :deep(.el-card__body) {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.kb-selector {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

.kb-label {
  font-size: 12px;
  color: #909399;
}

.message-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 18px 20px;
  background: #fafbfc;
}

.welcome {
  text-align: center;
  padding: 60px 20px;
  color: #909399;
}

.welcome-icon {
  font-size: 48px;
  color: #c0c4cc;
}

.welcome h3 {
  margin: 14px 0 8px;
  font-size: 16px;
  color: #303133;
}

.welcome p {
  margin: 0;
  font-size: 13px;
  line-height: 1.8;
}

.examples {
  margin-top: 26px;
}

.examples-title {
  font-size: 12px;
  margin-bottom: 10px;
}

.example-tag {
  margin: 4px;
  cursor: pointer;
}

.example-tag:hover {
  background: #ecf5ff;
  border-color: #409eff;
  color: #409eff;
}

.message-row {
  display: flex;
  gap: 10px;
  margin-bottom: 20px;
}

.message-row.user {
  flex-direction: row-reverse;
}

.avatar {
  flex-shrink: 0;
  width: 34px;
  height: 34px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: #ecf5ff;
  color: #409eff;
}

.message-row.user .avatar {
  background: #f0f9eb;
  color: #67c23a;
}

.bubble-wrap {
  max-width: 78%;
}

.bubble {
  padding: 11px 15px;
  border-radius: 10px;
  font-size: 14px;
}

.user-bubble {
  background: #409eff;
  color: #fff;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}

.assistant-bubble {
  background: #fff;
  border: 1px solid #ebeef5;
}

.thinking {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #909399;
  font-size: 13px;
}

.assistant-bubble.failed {
  border-color: #fbc4c4;
}

/* 打字光标：一个闪烁的竖线，让用户明确知道"还在生成" */
.typing-cursor {
  display: inline-block;
  width: 2px;
  height: 15px;
  margin-left: 2px;
  vertical-align: text-bottom;
  background: #409eff;
  animation: blink 1s step-end infinite;
}

@keyframes blink {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0;
  }
}

.marker {
  display: block;
  margin-top: 10px;
}

.actions {
  display: flex;
  gap: 8px;
}

.bubble-footer {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed #f0f0f0;
}

.latency {
  margin-left: auto;
  font-size: 11px;
  color: #c0c4cc;
}

/* ---------- 点赞/点踩 ---------- */
.feedback-actions {
  display: inline-flex;
  align-items: center;
  gap: 10px;
}

.feedback-icon {
  font-size: 16px;
  color: #c0c4cc;
  cursor: pointer;
  transition: color 0.15s, transform 0.1s;
}

.feedback-icon:hover {
  color: #409eff;
  /* 轻微放大，让"点中了"这件事有反馈感 */
  transform: scale(1.15);
}

/* 已经点过赞：蓝色实心高亮。
   刷新页面后会从后端带回这个状态，不会退回灰色 */
.feedback-icon.active {
  color: #409eff;
}

.feedback-icon.dislike:hover,
.feedback-icon.dislike.active {
  color: #f56c6c;
}

/* ---------- 输入区 ---------- */
.input-area {
  padding: 12px 16px;
  border-top: 1px solid #ebeef5;
  background: #fff;
}

.input-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 10px;
}

.hint {
  font-size: 12px;
  color: #909399;
}

/* ---------- 右侧引用面板 ---------- */
.cite-panel {
  flex: 0 0 320px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.cite-panel :deep(.el-card__body) {
  flex: 1;
  overflow: hidden;
}
</style>
