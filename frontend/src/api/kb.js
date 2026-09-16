/**
 * 知识库接口。
 *
 * 权限说明：列表查询所有登录用户都能调（问答页要选知识库），
 * 增删改只有管理员可用——如果普通用户调用会收到 403，
 * 前端守卫已经挡了一层，这里再挡一层（后端才是真正的防线）。
 */
import request from './request'

/** 分页获取知识库列表 */
export function listKnowledgeBases({ page = 1, pageSize = 20, keyword = '' } = {}) {
  return request.get('/knowledge-bases', {
    params: { page, page_size: pageSize, keyword: keyword || undefined },
  })
}

/** 获取全部知识库（不分页），问答页的知识库下拉框用 */
export function listAllKnowledgeBases() {
  return request.get('/knowledge-bases/all')
}

/** 知识库详情 */
export function getKnowledgeBase(kbId) {
  return request.get(`/knowledge-bases/${kbId}`)
}

/** 新建知识库（仅管理员） */
export function createKnowledgeBase(name, description) {
  return request.post('/knowledge-bases', { name, description })
}

/** 修改知识库（仅管理员） */
export function updateKnowledgeBase(kbId, { name, description }) {
  return request.patch(`/knowledge-bases/${kbId}`, { name, description })
}

/** 删除知识库（仅管理员）。会连带删除它下面的所有文档和向量数据 */
export function deleteKnowledgeBase(kbId) {
  return request.delete(`/knowledge-bases/${kbId}`)
}

/**
 * 获取知识库的处理状态概览（仅管理员）
 *
 * 返回 { total, by_status, in_progress, has_running_tasks }
 * 页面靠 has_running_tasks 判断要不要继续轮询。
 */
export function getKnowledgeBaseStatus(kbId) {
  return request.get(`/knowledge-bases/${kbId}/status`)
}
