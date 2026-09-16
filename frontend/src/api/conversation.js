/**
 * 会话管理接口。
 *
 * 所有接口都只操作当前登录用户自己的会话——
 * 服务端会校验归属，拿别人的会话 ID 来调用会返回 404。
 */
import request from './request'

/** 我的会话列表（分页） */
export function listConversations({ page = 1, pageSize = 50, keyword = '' } = {}) {
  return request.get('/conversations', {
    params: { page, page_size: pageSize, keyword: keyword || undefined },
  })
}

/** 新建会话 */
export function createConversation({ title, kbId } = {}) {
  return request.post('/conversations', { title: title ?? null, kb_id: kbId ?? null })
}

/**
 * 拉取会话的历史消息。
 *
 * @param {number} conversationId
 * @param {object} [options]
 * @param {number} [options.limit] 最多取多少条
 * @param {number} [options.beforeId] 取这个 ID 之前的消息（往上翻历史时用）
 */
export function listMessages(conversationId, { limit = 50, beforeId } = {}) {
  return request.get(`/conversations/${conversationId}/messages`, {
    params: { limit, before_id: beforeId ?? undefined },
  })
}

/** 重命名会话 */
export function updateConversation(conversationId, { title, kbId } = {}) {
  return request.patch(`/conversations/${conversationId}`, {
    title: title ?? null,
    kb_id: kbId ?? null,
  })
}

/** 删除会话（它下面的消息会一并删除） */
export function deleteConversation(conversationId) {
  return request.delete(`/conversations/${conversationId}`)
}

/** 清空我的全部会话 */
export function clearConversations() {
  return request.delete('/conversations')
}
