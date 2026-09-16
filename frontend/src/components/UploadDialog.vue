<script setup>
/**
 * 文档上传对话框。
 *
 * 交互设计：选文件时先在本地校验（类型、大小），不合格的直接拦下来并说明原因，
 * 不要等传到服务器才报错——传一个 100MB 的文件浪费的是用户的时间和带宽。
 *
 * 上传完成后接口会立刻返回，此时文档还只是"待处理"，
 * 真正的解析和向量化在后台跑。所以上传成功后要把结果告诉父组件，
 * 由父组件去轮询刷新列表看进度。
 */
import { ElMessage } from 'element-plus'
import { computed, ref, watch } from 'vue'

import { ACCEPTED_EXTENSIONS, MAX_FILE_SIZE_MB, uploadDocuments } from '@/api/document'
import { formatFileSize } from '@/utils/format'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  kbId: { type: Number, default: null },
  kbName: { type: String, default: '' },
})

const emit = defineEmits(['update:modelValue', 'uploaded'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const uploadRef = ref()
const fileList = ref([])
const uploading = ref(false)
const progress = ref(0)
const results = ref([]) // 上传结果，全部成功后展示给用户看

/** 提交按钮是否可用 */
const canSubmit = computed(() => fileList.value.length > 0 && !uploading.value)

/** 关闭对话框时把状态清干净，避免下次打开还残留上次的文件 */
watch(visible, (open) => {
  if (!open) {
    reset()
  }
})

/**
 * 只清空"已选文件"，**保留上传结果**。
 *
 * 为什么要把这两件事拆开：上传失败后，对话框要留在原地让用户看到失败原因。
 * 如果这里顺手把 results 也清了，用户就只看到一句"请看下方原因"、
 * 而下方一片空白——完全不知道哪里出了问题。
 * （之前就是这么写的，那段"有失败的留在对话框里让用户看原因"的注释
 *   实际上从来没生效过。）
 */
function clearFiles() {
  fileList.value = []
  progress.value = 0
  uploadRef.value?.clearFiles()
}

/** 彻底重置。关闭对话框时调，保证下次打开是干净的 */
function reset() {
  clearFiles()
  results.value = []
  uploading.value = false
}

/**
 * 选择文件时的本地校验。
 *
 * 用 el-upload 的 before-upload 没用，因为我们是手动提交、不走它的自动上传流程，
 * 所以要在 on-change 里自己判断。
 */
function handleChange(uploadFile, uploadFiles) {
  const file = uploadFile.raw
  if (!file) return

  const ext = `.${file.name.split('.').pop().toLowerCase()}`

  if (!ACCEPTED_EXTENSIONS.includes(ext)) {
    ElMessage.warning(`「${file.name}」类型不支持，只接受 ${ACCEPTED_EXTENSIONS.join('、')}`)
    removeFile(uploadFile)
    return
  }

  const sizeMB = file.size / 1024 / 1024
  if (sizeMB > MAX_FILE_SIZE_MB) {
    ElMessage.warning(
      `「${file.name}」有 ${sizeMB.toFixed(1)}MB，超过 ${MAX_FILE_SIZE_MB}MB 上限`,
    )
    removeFile(uploadFile)
    return
  }

  fileList.value = uploadFiles
}

function removeFile(uploadFile) {
  uploadRef.value?.handleRemove(uploadFile)
  fileList.value = fileList.value.filter((f) => f.uid !== uploadFile.uid)
}

function handleRemove(uploadFile, uploadFiles) {
  fileList.value = uploadFiles
}

async function submit() {
  if (!canSubmit.value) return

  const files = fileList.value.map((f) => f.raw).filter(Boolean)
  if (files.length === 0) {
    ElMessage.warning('请先选择文件')
    return
  }

  uploading.value = true
  progress.value = 0

  try {
    const data = await uploadDocuments(props.kbId, files, (p) => {
      progress.value = p
    })

    results.value = data

    // 后端对每个文件会返回三种结果之一，不能只按 success 分成两类：
    //   created    —— 新文件，正在后台处理
    //   duplicated —— 内容重复被跳过。**success 也是 true**，
    //                 但说成"上传成功"会让用户以为资料入库了两份
    //   failed     —— 真的失败了
    const created = data.filter((r) => r.result === 'created').length
    const duplicated = data.filter((r) => r.result === 'duplicated').length
    const failCount = data.filter((r) => !r.success).length

    if (failCount === 0 && duplicated === 0) {
      ElMessage.success(`${created} 个文件上传成功，正在后台处理`)
    } else if (failCount === 0) {
      ElMessage.success(`${created} 个上传成功，${duplicated} 个内容重复已跳过`)
    } else if (created + duplicated === 0) {
      ElMessage.error('全部文件上传失败，请看下方原因')
    } else {
      ElMessage.warning(
        `${created} 个成功、${failCount} 个失败` +
          (duplicated ? `、${duplicated} 个内容重复跳过` : ''),
      )
    }

    // 通知父组件刷新列表并开始轮询进度
    emit('uploaded')

    if (failCount === 0) {
      // 全部成功就直接关掉（结果区会先闪一下让用户看到）
      setTimeout(() => {
        visible.value = false
      }, 800)
    } else {
      // 有失败的：**只清空已选文件，保留上传结果**。
      // 用户需要看到每一份文件具体失败在什么地方，
      // 这里要是连结果一起清掉（之前就是这么写的），
      // 提示语"请看下方原因"指向的就是一片空白
      clearFiles()
    }
  } catch {
    // 具体错误由 request.js 的拦截器统一提示
  } finally {
    uploading.value = false
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="上传商品资料"
    width="620px"
    :close-on-click-modal="false"
    :close-on-press-escape="!uploading"
  >
    <el-alert v-if="kbName" type="info" :closable="false" show-icon class="kb-tip">
      资料将上传到知识库：<strong>{{ kbName }}</strong>
    </el-alert>

    <el-upload
      ref="uploadRef"
      drag
      multiple
      :auto-upload="false"
      :accept="ACCEPTED_EXTENSIONS.join(',')"
      :file-list="fileList"
      :on-change="handleChange"
      :on-remove="handleRemove"
      :disabled="uploading"
    >
      <el-icon class="upload-icon"><UploadFilled /></el-icon>
      <div class="upload-text">把文件拖到这里，或<em>点击选择</em></div>
      <template #tip>
        <div class="upload-tip">
          支持 {{ ACCEPTED_EXTENSIONS.join('、') }}，单个文件不超过 {{ MAX_FILE_SIZE_MB }}MB，可一次选多个
          <br />
          上传后系统会自动解析、切分并建立索引，处理进度可在列表中查看
        </div>
      </template>
    </el-upload>

    <!-- 已选文件预览 -->
    <div v-if="fileList.length > 0" class="file-summary">
      已选 {{ fileList.length }} 个文件，合计
      {{ formatFileSize(fileList.reduce((sum, f) => sum + (f.raw?.size || 0), 0)) }}
    </div>

    <!-- 上传进度 -->
    <el-progress
      v-if="uploading"
      :percentage="progress"
      :stroke-width="14"
      striped
      striped-flow
      class="progress"
    />

    <!-- 上传结果明细：有失败项时才展示 -->
    <div v-if="results.length > 0 && !uploading" class="results">
      <el-divider content-position="left">上传结果</el-divider>
      <!--
        结果分三种，图标和颜色都要区分开：
          失败    —— 红色叉，最需要被看到
          重复跳过 —— 黄色叹号。它有 success=true，但**不能用绿色的勾**，
                     否则用户会以为同一份资料入库了两遍
          成功    —— 绿色勾
      -->
      <div v-for="(item, index) in results" :key="index" class="result-item">
        <template v-if="!item.success">
          <el-icon class="bad"><CircleClose /></el-icon>
          <span class="result-name">{{ item.filename }}</span>
          <span class="result-msg bad">{{ item.message }}</span>
        </template>
        <template v-else-if="item.result === 'duplicated'">
          <el-icon class="warn"><WarningFilled /></el-icon>
          <span class="result-name">{{ item.filename }}</span>
          <span class="result-msg warn">{{ item.message }}</span>
        </template>
        <template v-else>
          <el-icon class="ok"><CircleCheck /></el-icon>
          <span class="result-name">{{ item.filename }}</span>
          <span class="result-msg">{{ item.message }}</span>
        </template>
      </div>
    </div>

    <template #footer>
      <div class="footer">
        <span class="hint">上传后请留意列表中的处理状态</span>
        <div>
          <el-button :disabled="uploading" @click="visible = false">取消</el-button>
          <el-button type="primary" :loading="uploading" :disabled="!canSubmit" @click="submit">
            {{ uploading ? `上传中 ${progress}%` : `开始上传（${fileList.length}）` }}
          </el-button>
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.kb-tip {
  margin-bottom: 14px;
}

.upload-icon {
  font-size: 52px;
  color: #c0c4cc;
  margin-top: 24px;
}

.upload-text {
  color: #606266;
  margin-top: 6px;
}

.upload-text em {
  color: #409eff;
  font-style: normal;
}

.upload-tip {
  margin-top: 10px;
  font-size: 12px;
  color: #909399;
  line-height: 1.7;
}

.file-summary {
  margin-top: 12px;
  font-size: 13px;
  color: #606266;
}

.progress {
  margin-top: 14px;
}

.results {
  margin-top: 8px;
  max-height: 200px;
  overflow-y: auto;
}

.result-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
  font-size: 13px;
  border-bottom: 1px dashed #f0f0f0;
}

.result-name {
  flex: 0 0 auto;
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #303133;
}

.result-msg {
  color: #909399;
  font-size: 12px;
}

.ok {
  color: #67c23a;
}

.bad {
  color: #f56c6c;
}

/* 「内容重复被跳过」用的颜色。单独一档是必要的：
   它不是失败（资料确实已经在库里了），但也不能算成功
   （用户以为传了新资料，其实一份都没入库） */
.warn {
  color: #e6a23c;
}

.footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.hint {
  font-size: 12px;
  color: #909399;
}
</style>
