<script setup>
/**
 * 知识库管理页（仅管理员可访问）。
 *
 * 页面分左右两块：
 *   左边 —— 知识库列表，可以新建、重命名、删除
 *   右边 —— 选中知识库里的文档列表，可以上传、删除、重建索引、查看分块
 *
 * 有一个自动轮询机制：只要列表里还有文档在处理中（待处理/解析中/分块中/向量化中），
 * 就每 2 秒刷新一次列表。全部处理完自动停止，不做无谓的请求。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, onUnmounted, ref } from 'vue'

import {
  createKnowledgeBase,
  deleteKnowledgeBase,
  getKnowledgeBaseStatus,
  listKnowledgeBases,
  updateKnowledgeBase,
} from '@/api/kb'
import { deleteDocument, getDocumentChunks, listDocuments, reindexDocument } from '@/api/document'
import UploadDialog from '@/components/UploadDialog.vue'
import {
  DOCUMENT_STATUS_OPTIONS,
  formatFileSize,
  formatRelativeTime,
  getStatusConfig,
} from '@/utils/format'

// ---------- 状态 ----------
const loadingKb = ref(false)
const loadingDocs = ref(false)

const kbList = ref([])
const currentKb = ref(null)

const documents = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(10)
const statusFilter = ref('')
const keyword = ref('')

const uploadVisible = ref(false)

// 分块预览
const chunkDrawerVisible = ref(false)
const chunkLoading = ref(false)
const chunkData = ref({ total: 0, items: [] })
const chunkDoc = ref(null)

/** 是否还有文档在处理中——决定要不要继续轮询 */
const hasRunning = computed(() =>
  documents.value.some((d) => ['pending', 'parsing', 'chunking', 'embedding'].includes(d.status)),
)

let pollTimer = null

// ---------- 知识库 ----------
async function loadKnowledgeBases(selectId = null) {
  loadingKb.value = true
  try {
    const data = await listKnowledgeBases({ pageSize: 100 })
    kbList.value = data.items

    // 优先选中指定的那个；否则保持当前选中；再否则选第一个
    const target =
      kbList.value.find((k) => k.id === selectId) ||
      kbList.value.find((k) => k.id === currentKb.value?.id) ||
      kbList.value[0] ||
      null

    currentKb.value = target

    if (target) {
      await loadDocuments()
    } else {
      documents.value = []
      total.value = 0
    }
  } finally {
    loadingKb.value = false
  }
}

async function handleSelectKb(kb) {
  if (currentKb.value?.id === kb.id) return
  currentKb.value = kb
  page.value = 1
  statusFilter.value = ''
  keyword.value = ''
  stopPolling()
  await loadDocuments()
}

async function handleCreateKb() {
  try {
    const { value } = await ElMessageBox.prompt('请输入知识库名称', '新建知识库', {
      confirmButtonText: '创建',
      cancelButtonText: '取消',
      inputPattern: /\S/,
      inputErrorMessage: '名称不能为空',
      inputPlaceholder: '例如：手机数码、家用电器',
    })

    const kb = await createKnowledgeBase(value.trim(), null)
    ElMessage.success('知识库创建成功')
    await loadKnowledgeBases(kb.id)
  } catch (e) {
    // 用户点取消会进这里，不需要提示
    if (e !== 'cancel' && e !== 'close') {
      // 真正的错误已由拦截器提示
    }
  }
}

async function handleRenameKb() {
  if (!currentKb.value) return
  try {
    const { value } = await ElMessageBox.prompt('请输入新的名称', '重命名知识库', {
      confirmButtonText: '保存',
      cancelButtonText: '取消',
      inputValue: currentKb.value.name,
      inputPattern: /\S/,
      inputErrorMessage: '名称不能为空',
    })

    await updateKnowledgeBase(currentKb.value.id, { name: value.trim() })
    ElMessage.success('重命名成功')
    await loadKnowledgeBases()
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') {
      // 已由拦截器提示
    }
  }
}

async function handleDeleteKb() {
  if (!currentKb.value) return

  try {
    await ElMessageBox.confirm(
      `确定要删除知识库「${currentKb.value.name}」吗？` +
        `它下面的 ${currentKb.value.doc_count} 份文档和全部索引都会被一并清除，且无法恢复。`,
      '删除知识库',
      { confirmButtonText: '确定删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }

  await deleteKnowledgeBase(currentKb.value.id)
  ElMessage.success('知识库已删除')
  currentKb.value = null
  await loadKnowledgeBases()
}

// ---------- 文档 ----------
async function loadDocuments() {
  if (!currentKb.value) return

  loadingDocs.value = true
  try {
    const data = await listDocuments({
      kbId: currentKb.value.id,
      page: page.value,
      pageSize: pageSize.value,
      status: statusFilter.value,
      keyword: keyword.value,
    })
    documents.value = data.items
    total.value = data.total

    // 拉完立刻判断要不要开轮询：有在处理中的就定时刷新，没有就停掉
    syncPolling()
  } finally {
    loadingDocs.value = false
  }
}

/**
 * 根据当前列表里有没有处理中的文档，决定开启还是停止轮询。
 *
 * 为什么不做成"一直轮询"：用户开着页面不动的时候，
 * 每 2 秒发一次请求纯属浪费，还会白占服务器资源。
 */
function syncPolling() {
  if (hasRunning.value) {
    if (!pollTimer) {
      pollTimer = setInterval(() => {
        loadDocuments()
      }, 2000)
    }
  } else {
    stopPolling()
    // 处理完成后，知识库的文档数和分块数会变，顺手刷新左侧列表的数字
    if (currentKb.value) {
      getKnowledgeBaseStatus(currentKb.value.id)
        .then(() => refreshKbCounters())
        .catch(() => {})
    }
  }
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

/** 刷新左侧知识库列表上的文档数/分块数 */
async function refreshKbCounters() {
  try {
    const data = await listKnowledgeBases({ pageSize: 100 })
    kbList.value = data.items
    const updated = kbList.value.find((k) => k.id === currentKb.value?.id)
    if (updated) currentKb.value = updated
  } catch {
    // 刷新计数失败不影响主流程，静默处理
  }
}

function handleSearch() {
  page.value = 1
  loadDocuments()
}

function handleResetFilter() {
  statusFilter.value = ''
  keyword.value = ''
  page.value = 1
  loadDocuments()
}

async function handleDeleteDoc(doc) {
  try {
    await ElMessageBox.confirm(
      `确定要删除「${doc.filename}」吗？它的全部索引数据会一并清除。`,
      '删除文档',
      { confirmButtonText: '确定删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }

  await deleteDocument(doc.id)
  ElMessage.success('文档已删除')
  await loadDocuments()
  await refreshKbCounters()
}

async function handleReindex(doc) {
  await reindexDocument(doc.id)
  ElMessage.success('已开始重建索引，请留意处理状态')
  await loadDocuments()
}

async function handleViewChunks(doc) {
  chunkDoc.value = doc
  chunkDrawerVisible.value = true
  chunkLoading.value = true
  try {
    const data = await getDocumentChunks(doc.id, { limit: 20, offset: 0 })
    chunkData.value = data
  } finally {
    chunkLoading.value = false
  }
}

function handleUploaded() {
  loadDocuments()
}

// ---------- 生命周期 ----------
onMounted(() => {
  loadKnowledgeBases()
})

onUnmounted(() => {
  // 离开页面一定要清掉定时器，否则会一直发请求，而且组件销毁后还会报错
  stopPolling()
})
</script>

<template>
  <div class="knowledge-page">
    <!-- 左：知识库列表 -->
    <el-card class="kb-panel" shadow="never" body-style="padding:0">
      <template #header>
        <div class="panel-header">
          <span class="panel-title">知识库</span>
          <el-button type="primary" size="small" @click="handleCreateKb">
            <el-icon><Plus /></el-icon>新建
          </el-button>
        </div>
      </template>

      <div v-loading="loadingKb" class="kb-list">
        <div v-if="kbList.length === 0 && !loadingKb" class="empty-tip">
          <el-empty description="还没有知识库" :image-size="80">
            <el-button type="primary" size="small" @click="handleCreateKb">
              创建第一个知识库
            </el-button>
          </el-empty>
        </div>

        <div
          v-for="kb in kbList"
          :key="kb.id"
          class="kb-item"
          :class="{ active: currentKb?.id === kb.id }"
          @click="handleSelectKb(kb)"
        >
          <div class="kb-name">
            <el-icon><Collection /></el-icon>
            <span class="name-text">{{ kb.name }}</span>
          </div>
          <div class="kb-meta">
            {{ kb.doc_count }} 份资料 · {{ kb.chunk_count }} 个分块
          </div>
        </div>
      </div>
    </el-card>

    <!-- 右：文档列表 -->
    <el-card class="doc-panel" shadow="never">
      <template #header>
        <div class="panel-header">
          <div class="panel-title">
            {{ currentKb ? `${currentKb.name} · 文档列表` : '文档列表' }}
            <el-tag v-if="hasRunning" type="warning" size="small" class="running-tag">
              正在处理中，列表自动刷新
            </el-tag>
          </div>
          <div class="header-actions">
            <el-button
              v-if="currentKb"
              size="small"
              :disabled="!currentKb"
              @click="handleRenameKb"
            >
              <el-icon><EditPen /></el-icon>重命名
            </el-button>
            <el-button v-if="currentKb" size="small" type="danger" plain @click="handleDeleteKb">
              <el-icon><Delete /></el-icon>删除知识库
            </el-button>
            <el-button
              type="primary"
              size="small"
              :disabled="!currentKb"
              @click="uploadVisible = true"
            >
              <el-icon><Upload /></el-icon>上传资料
            </el-button>
          </div>
        </div>
      </template>

      <div v-if="!currentKb" class="no-kb-tip">
        <el-empty description="请先在左侧选择一个知识库" :image-size="100" />
      </div>

      <template v-else>
        <!-- 筛选栏 -->
        <div class="filter-bar">
          <el-input
            v-model="keyword"
            placeholder="搜索文件名"
            clearable
            size="default"
            style="width: 220px"
            @keyup.enter="handleSearch"
            @clear="handleSearch"
          >
            <template #prefix><el-icon><Search /></el-icon></template>
          </el-input>

          <el-select
            v-model="statusFilter"
            placeholder="全部状态"
            clearable
            size="default"
            style="width: 140px"
            @change="handleSearch"
          >
            <el-option
              v-for="opt in DOCUMENT_STATUS_OPTIONS"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>

          <el-button size="default" @click="handleSearch">
            <el-icon><Search /></el-icon>查询
          </el-button>
          <el-button size="default" @click="handleResetFilter">重置</el-button>

          <div class="filter-spacer" />

          <el-button size="default" :loading="loadingDocs" @click="loadDocuments">
            <el-icon><Refresh /></el-icon>刷新
          </el-button>
        </div>

        <!-- 文档表格 -->
        <el-table v-loading="loadingDocs" :data="documents" stripe class="doc-table">
          <el-table-column prop="filename" label="文件名" min-width="220" show-overflow-tooltip>
            <template #default="{ row }">
              <el-icon class="file-icon"><Document /></el-icon>
              {{ row.filename }}
            </template>
          </el-table-column>

          <el-table-column label="类型" width="80">
            <template #default="{ row }">
              <el-tag size="small" effect="plain">{{ row.file_type.replace('.', '') }}</el-tag>
            </template>
          </el-table-column>

          <el-table-column label="大小" width="100">
            <template #default="{ row }">{{ formatFileSize(row.file_size) }}</template>
          </el-table-column>

          <el-table-column label="状态" width="180">
            <template #default="{ row }">
              <el-tooltip
                :content="row.error_msg || ''"
                :disabled="!row.error_msg"
                placement="top"
              >
                <el-tag :type="getStatusConfig(row.status).type" size="small">
                  <el-icon
                    v-if="['parsing', 'chunking', 'embedding'].includes(row.status)"
                    class="is-loading"
                  >
                    <Loading />
                  </el-icon>
                  {{ getStatusConfig(row.status).label }}
                </el-tag>
              </el-tooltip>
            </template>
          </el-table-column>

          <el-table-column label="分块数" width="90">
            <template #default="{ row }">
              <span v-if="row.chunk_count > 0">{{ row.chunk_count }}</span>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>

          <el-table-column label="上传时间" width="130">
            <template #default="{ row }">
              <el-tooltip :content="row.created_at" placement="top">
                <span>{{ formatRelativeTime(row.created_at) }}</span>
              </el-tooltip>
            </template>
          </el-table-column>

          <el-table-column label="操作" width="230" fixed="right">
            <template #default="{ row }">
              <el-button
                link
                type="primary"
                size="small"
                :disabled="row.status !== 'ready'"
                @click="handleViewChunks(row)"
              >
                查看分块
              </el-button>
              <el-button
                link
                type="warning"
                size="small"
                :disabled="row.status !== 'failed' && row.status !== 'ready'"
                @click="handleReindex(row)"
              >
                重建索引
              </el-button>
              <el-button link type="danger" size="small" @click="handleDeleteDoc(row)">
                删除
              </el-button>
            </template>
          </el-table-column>

          <template #empty>
            <el-empty description="还没有上传任何资料" :image-size="90">
              <el-button type="primary" size="small" @click="uploadVisible = true">
                上传第一份资料
              </el-button>
            </el-empty>
          </template>
        </el-table>

        <!-- 分页 -->
        <div v-if="total > 0" class="pagination">
          <el-pagination
            v-model:current-page="page"
            v-model:page-size="pageSize"
            :total="total"
            :page-sizes="[10, 20, 50, 100]"
            layout="total, sizes, prev, pager, next, jumper"
            background
            @current-change="loadDocuments"
            @size-change="handleSearch"
          />
        </div>
      </template>
    </el-card>

    <!-- 上传对话框 -->
    <UploadDialog
      v-model="uploadVisible"
      :kb-id="currentKb?.id"
      :kb-name="currentKb?.name"
      @uploaded="handleUploaded"
    />

    <!-- 分块预览抽屉 -->
    <el-drawer v-model="chunkDrawerVisible" size="46%" :title="`分块预览 · ${chunkDoc?.filename || ''}`">
      <div v-loading="chunkLoading" class="chunk-body">
        <el-alert
          title="这里展示的是文档被切分后的实际内容。切得太碎或太整，都说明分块参数需要调整。"
          type="info"
          :closable="false"
          show-icon
          class="chunk-tip"
        />

        <div class="chunk-summary">
          共 {{ chunkData.total }} 个分块，下面展示前 {{ chunkData.items.length }} 个
        </div>

        <div v-for="(item, index) in chunkData.items" :key="item.id" class="chunk-item">
          <div class="chunk-head">
            <el-tag size="small" type="primary" effect="plain">#{{ index + 1 }}</el-tag>
            <span class="chunk-id">{{ item.id }}</span>
            <span class="chunk-len">{{ item.content.length }} 字</span>
          </div>
          <pre class="chunk-text">{{ item.content }}</pre>
        </div>

        <el-empty v-if="!chunkLoading && chunkData.items.length === 0" description="暂无分块" />
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.knowledge-page {
  display: flex;
  gap: 14px;
  height: 100%;
}

/* ---------- 左侧知识库列表 ---------- */
.kb-panel {
  flex: 0 0 260px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.kb-panel :deep(.el-card__body) {
  height: 100%;
  overflow-y: auto;
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.panel-title {
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.running-tag {
  font-weight: normal;
}

.header-actions {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}

.kb-list {
  padding: 8px;
  min-height: 200px;
}

.kb-item {
  padding: 10px 12px;
  border-radius: 6px;
  cursor: pointer;
  transition: background-color 0.2s;
  margin-bottom: 4px;
}

.kb-item:hover {
  background-color: #f5f7fa;
}

.kb-item.active {
  background-color: #ecf5ff;
}

.kb-name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  color: #303133;
  font-weight: 500;
}

.name-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kb-meta {
  margin-top: 4px;
  margin-left: 20px;
  font-size: 12px;
  color: #909399;
}

/* ---------- 右侧文档区 ---------- */
.doc-panel {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.doc-panel :deep(.el-card__body) {
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.filter-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.filter-spacer {
  flex: 1;
}

.doc-table {
  flex: 1;
  overflow: auto;
}

.file-icon {
  color: #909399;
  margin-right: 4px;
  vertical-align: middle;
}

.muted {
  color: #c0c4cc;
}

.pagination {
  margin-top: 12px;
  display: flex;
  justify-content: flex-end;
}

.no-kb-tip {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}

.empty-tip {
  padding: 20px 0;
}

/* ---------- 分块预览 ---------- */
.chunk-body {
  padding: 0 4px;
}

.chunk-tip {
  margin-bottom: 12px;
}

.chunk-summary {
  font-size: 13px;
  color: #606266;
  margin-bottom: 12px;
}

.chunk-item {
  border: 1px solid #ebeef5;
  border-radius: 6px;
  padding: 10px 12px;
  margin-bottom: 10px;
}

.chunk-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.chunk-id {
  font-size: 12px;
  color: #c0c4cc;
  font-family: Consolas, monospace;
}

.chunk-len {
  margin-left: auto;
  font-size: 12px;
  color: #909399;
}

.chunk-text {
  margin: 0;
  font-family: inherit;
  font-size: 13px;
  line-height: 1.75;
  color: #303133;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
