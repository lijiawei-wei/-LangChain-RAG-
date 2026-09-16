<script setup>
/**
 * 引用来源面板：展示某条回答用到了知识库里的哪些片段。
 *
 * 两个设计取舍：
 *
 * 1. **把「被引用的」和「只是检索到的」分开**。
 *    检索通常返回 5 条，但模型可能只用了其中 1 条。如果全平铺出来，
 *    用户会以为 5 条都是依据，还得自己一条条核对。默认只展示被引用的，
 *    其余折叠起来——想看的人再点开。
 *
 * 2. **模型没能回答时明确说明**。
 *    如果回答是「根据现有资料无法回答」，那它一条引用都没标。
 *    这时候要如实告诉用户「本次回答未引用知识库内容」，
 *    而不是把 5 条检索结果摆出来，制造"有依据"的假象。
 */
import { computed, ref, watch } from 'vue'

const props = defineProps({
  citations: { type: Array, default: () => [] },
  /** 要高亮的那一条（用户点击回答里的 [n] 角标时传入） */
  highlightIndex: { type: Number, default: null },
})

const showAll = ref(false)

/** 被回答真正引用到的来源 */
const usedCitations = computed(() => props.citations.filter((c) => c.used))

/** 只是检索到、但回答里没用到的来源 */
const unusedCitations = computed(() => props.citations.filter((c) => !c.used))

/** 当前要展示的列表 */
const displayList = computed(() =>
  showAll.value ? props.citations : usedCitations.value,
)

/** 高亮的那条自动滚进视野 */
const highlightRef = ref(null)

watch(
  () => props.highlightIndex,
  () => {
    // 等 DOM 更新完再滚动
    setTimeout(() => {
      highlightRef.value?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }, 50)
  },
)

/** 相关度分数的显示颜色：越相关颜色越绿 */
function scoreType(score) {
  if (score >= 0.7) return 'success'
  if (score >= 0.5) return 'warning'
  return 'info'
}
</script>

<template>
  <div class="citation-panel">
    <!-- 一条引用都没有 -->
    <el-empty
      v-if="citations.length === 0"
      description="这条回答没有引用知识库内容"
      :image-size="80"
    />

    <template v-else>
      <!-- 模型没有标任何引用 -->
      <el-alert
        v-if="usedCitations.length === 0"
        type="warning"
        :closable="false"
        show-icon
        class="no-citation-tip"
      >
        <template #title>本次回答未引用知识库内容</template>
        <template #default>
          下面的 {{ citations.length }} 条是检索到的资料，但回答里没有标注引用它们，
          说明这次回答可能没有依据。请谨慎参考。
        </template>
      </el-alert>

      <div v-else class="panel-summary">
        这条回答引用了 <strong>{{ usedCitations.length }}</strong> 条资料
        <span v-if="unusedCitations.length > 0" class="extra">
          （另有 {{ unusedCitations.length }} 条被检索到但未使用）
        </span>
      </div>

      <!-- 来源列表 -->
      <div class="citation-list">
        <div
          v-for="item in displayList"
          :key="item.index"
          :ref="(el) => { if (item.index === highlightIndex) highlightRef = el }"
          class="citation-item"
          :class="{
            highlighted: item.index === highlightIndex,
            unused: !item.used,
          }"
        >
          <div class="item-head">
            <span class="badge">[{{ item.index }}]</span>
            <span class="source-label" :title="item.source_label">
              {{ item.source_label }}
            </span>
            <el-tag :type="scoreType(item.score)" size="small" effect="plain" class="score">
              相关度 {{ item.score }}
            </el-tag>
          </div>

          <el-tag v-if="!item.used" size="small" type="info" class="unused-tag">
            未被引用
          </el-tag>

          <pre class="item-text">{{ item.text }}{{ item.truncated ? '…' : '' }}</pre>
        </div>
      </div>

      <!-- 折叠开关 -->
      <div v-if="unusedCitations.length > 0" class="toggle">
        <el-button link type="primary" size="small" @click="showAll = !showAll">
          {{ showAll ? '只看被引用的' : `展开全部 ${citations.length} 条` }}
        </el-button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.citation-panel {
  height: 100%;
  overflow-y: auto;
  padding: 4px;
}

.no-citation-tip {
  margin-bottom: 12px;
}

.no-citation-tip :deep(.el-alert__title) {
  font-size: 13px;
  font-weight: 600;
}

.no-citation-tip :deep(.el-alert__content) {
  font-size: 12px;
  line-height: 1.7;
}

.panel-summary {
  margin-bottom: 12px;
  font-size: 13px;
  color: #606266;
}

.panel-summary .extra {
  color: #909399;
  font-size: 12px;
}

.citation-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.citation-item {
  padding: 10px 12px;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  background: #fff;
  transition: all 0.2s;
}

.citation-item.highlighted {
  border-color: #409eff;
  box-shadow: 0 0 0 3px rgba(64, 158, 255, 0.12);
  background: #f5faff;
}

.citation-item.unused {
  opacity: 0.62;
}

.item-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.badge {
  flex-shrink: 0;
  font-size: 12px;
  font-weight: 600;
  color: #409eff;
}

.source-label {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: #303133;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.score {
  flex-shrink: 0;
}

.unused-tag {
  margin-bottom: 6px;
}

.item-text {
  margin: 0;
  font-family: inherit;
  font-size: 12.5px;
  line-height: 1.75;
  color: #606266;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 200px;
  overflow-y: auto;
}

.toggle {
  margin-top: 14px;
  text-align: center;
}
</style>
