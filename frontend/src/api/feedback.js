/**
 * 答案反馈（点赞/点踩）接口。
 *
 * 注意后端的语义是「覆盖」不是「新增」：
 * 同一个用户对同一条回答，库里始终只有一条记录。
 * 所以"改主意"要分两步走——
 *   想从赞改成踩：直接提交 -1 就会覆盖掉，不用先取消
 *   想取消评价  ：调 cancelFeedback
 * 这一点和"再点一次就取消"的按钮交互是对应的，见 Chat.vue 的 handleFeedback。
 */
import request from './request'

/**
 * 提交评价。
 * @param {number} messageId 助手回答的消息 ID
 * @param {1|-1} rating 1 = 点赞，-1 = 点踩
 * @param {string} [comment] 补充说明，可不填
 */
export function submitFeedback(messageId, rating, comment) {
  return request.post(`/messages/${messageId}/feedback`, {
    rating,
    comment: comment ?? null,
  })
}

/** 取消自己对某条回答的评价。 */
export function cancelFeedback(messageId) {
  return request.delete(`/messages/${messageId}/feedback`)
}
