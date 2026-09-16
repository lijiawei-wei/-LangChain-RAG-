/**
 * 文档接口。全部仅管理员可用。
 */
import request from './request'

/** 允许上传的文件类型（要和后端 config.py 的 ALLOWED_EXTENSIONS 保持一致） */
export const ACCEPTED_EXTENSIONS = ['.pdf', '.docx', '.xlsx', '.txt', '.md']

/** 单个文件大小上限（MB），和后端 MAX_UPLOAD_SIZE_MB 保持一致 */
export const MAX_FILE_SIZE_MB = 50

/**
 * 上传文档。
 *
 * @param {number} kbId 归属的知识库 ID
 * @param {File[]} files 文件列表
 * @param {(percent:number)=>void} onProgress 上传进度回调
 *
 * 注意：上传完成 ≠ 处理完成。接口返回时文档只是"已入库待处理"，
 * 真正的解析和向量化在后台跑，需要靠轮询列表接口看进度。
 */
export function uploadDocuments(kbId, files, onProgress) {
  const formData = new FormData()
  formData.append('kb_id', String(kbId))
  files.forEach((file) => formData.append('files', file))

  return request.post('/documents/upload', formData, {
    // 上传大文件比较慢，把超时放宽到 5 分钟
    timeout: 5 * 60 * 1000,
    // 不手动设置 Content-Type：浏览器需要自己加上 multipart 的 boundary，
    // 写死了反而会导致后端解析不出文件
    onUploadProgress(event) {
      if (onProgress && event.total) {
        onProgress(Math.round((event.loaded / event.total) * 100))
      }
    },
  })
}

/** 分页查询文档列表 */
export function listDocuments({ kbId, page = 1, pageSize = 20, status = '', keyword = '' } = {}) {
  return request.get('/documents', {
    params: {
      kb_id: kbId,
      page,
      page_size: pageSize,
      status: status || undefined,
      keyword: keyword || undefined,
    },
  })
}

/** 查看某份文档被切成了哪些块 */
export function getDocumentChunks(docId, { limit = 20, offset = 0 } = {}) {
  return request.get(`/documents/${docId}/chunks`, { params: { limit, offset } })
}

/** 删除文档（同时清除它的向量和磁盘文件） */
export function deleteDocument(docId) {
  return request.delete(`/documents/${docId}`)
}

/** 重建索引：重新解析、分块、向量化 */
export function reindexDocument(docId) {
  return request.post(`/documents/${docId}/reindex`)
}
