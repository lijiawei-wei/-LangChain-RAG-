/**
 * 问答接口。
 */
import request from './request'

/**
 * 提问并获取回答（非流式，一次返回完整内容）。
 *
 * @param {object} params
 * @param {string} params.question 用户提问
 * @param {number} [params.conversationId] 会话 ID；不传会自动新建一个会话
 * @param {number} [params.kbId] 只检索某个知识库；不传表示检索全部
 *
 * 注意：模型生成回答比较慢（通常 1~3 秒），
 * 所以这里把超时单独放宽，不用默认的 30 秒。
 */
export function askQuestion({ question, conversationId, kbId }) {
  return request.post(
    '/chat/query',
    {
      question,
      conversation_id: conversationId ?? null,
      kb_id: kbId ?? null,
    },
    { timeout: 120000 },
  )
}
