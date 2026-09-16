/**
 * SSE（Server-Sent Events）客户端。
 *
 * 为什么不用浏览器原生的 EventSource：
 *   - EventSource 只能用 GET，而我们的问答接口是 POST（问题要通过请求体传）
 *   - EventSource 不能自定义请求头，没法带 Authorization 令牌
 * 所以只能用 fetch 发请求，然后自己读 ReadableStream、按 SSE 协议解析。
 */

const API_BASE = '/api/v1'

/**
 * 解析 SSE 数据块。
 *
 * SSE 协议里一条消息长这样（空行表示消息结束）：
 *     event: token
 *     data: {"delta":"你好"}
 *
 * 一次网络传输可能包含半条消息，也可能是三条半，所以必须用缓冲区
 * 累积起来、按空行切分，不能假设「一次读到一条完整消息」。
 */
export function parseSseBlock(block) {
  let event = 'message'
  const dataLines = []

  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) {
      event = line.slice(6).trim()
    } else if (line.startsWith('data:')) {
      // 按协议，data 后面可以跟多个空格，去掉一个即可
      dataLines.push(line.slice(5).replace(/^ /, ''))
    }
    // 以冒号开头的行是注释（心跳包），忽略
  }

  if (dataLines.length === 0) return null

  const raw = dataLines.join('\n')
  try {
    return { event, data: JSON.parse(raw) }
  } catch {
    // 不是 JSON 就当纯文本处理，避免因为一条异常数据丢掉整段回答
    return { event, data: { raw } }
  }
}

/**
 * 发起流式问答。
 *
 * @param {object} params
 * @param {string} params.question 用户提问
 * @param {number|null} params.conversationId 会话 ID
 * @param {number|null} params.kbId 知识库 ID
 * @param {string} params.token 访问令牌
 * @param {object} params.handlers 各事件的回调：
 *        onMeta / onSources / onToken / onDone / onError
 * @param {AbortSignal} [params.signal] 用于中断请求（用户点"停止生成"或离开页面）
 * @param {Function} [params.refreshToken] 令牌过期时的续期函数
 */
export async function streamChat({
  question,
  conversationId = null,
  kbId = null,
  token,
  handlers = {},
  signal,
  refreshToken,
}) {
  const { onMeta, onSources, onToken, onDone, onError } = handlers

  const doRequest = async (authToken) =>
    fetch(`${API_BASE}/chat/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${authToken}`,
      },
      body: JSON.stringify({
        question,
        conversation_id: conversationId,
        kb_id: kbId,
      }),
      signal,
    })

  let response = await doRequest(token)

  // 令牌过期：用刷新令牌换一张新的再试一次。
  // axios 有拦截器自动做这件事，但走 fetch 的请求绕过了拦截器，要手动处理。
  if (response.status === 401 && refreshToken) {
    try {
      const fresh = await refreshToken()
      response = await doRequest(fresh)
    } catch {
      onError?.({ message: '登录已过期，请重新登录', expired: true })
      return
    }
  }

  // 非 200：后端返回的是普通 JSON 错误，不是 SSE 流
  if (!response.ok) {
    let message = `请求失败（${response.status}）`
    try {
      const body = await response.json()
      message = body.message || body.detail || message
    } catch {
      // 响应体不是 JSON，用默认提示
    }
    onError?.({ message })
    return
  }

  if (!response.body) {
    onError?.({ message: '浏览器不支持流式响应，请更换 Chrome / Edge 等现代浏览器' })
    return
  }

  // ---------- 读取流 ----------
  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let finished = false

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })

      // 用空行切分消息。最后一段可能不完整，留在缓冲区等下一块数据
      const blocks = buffer.split('\n\n')
      buffer = blocks.pop() || ''

      for (const block of blocks) {
        if (!block.trim()) continue

        const parsed = parseSseBlock(block)
        if (!parsed) continue

        switch (parsed.event) {
          case 'meta':
            onMeta?.(parsed.data)
            break
          case 'sources':
            onSources?.(parsed.data)
            break
          case 'token':
            onToken?.(parsed.data.delta || '')
            break
          case 'done':
            finished = true
            onDone?.(parsed.data)
            break
          case 'error':
            finished = true
            onError?.(parsed.data)
            break
          default:
            // 未知事件类型直接忽略，保证向后兼容
            break
        }
      }
    }

    // 流正常结束但一直没收到 done / error，
    // 说明连接被中途掐断了（网络问题或服务重启），要告诉用户
    if (!finished && !signal?.aborted) {
      onError?.({ message: '连接意外中断，回答可能不完整，请重试' })
    }
  } catch (err) {
    // 用户主动中断（点了停止或切换会话）不算错误，静默处理
    if (err.name === 'AbortError') return
    onError?.({ message: `接收数据出错：${err.message}` })
  } finally {
    // 无论成功失败都要释放读取器，否则连接会一直挂着
    reader.cancel().catch(() => {})
  }
}
