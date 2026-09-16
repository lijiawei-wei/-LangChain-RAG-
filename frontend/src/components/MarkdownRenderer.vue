<script setup>
/**
 * Markdown 渲染组件，带引用角标和代码高亮。
 *
 * **安全提醒（重要）**：这里渲染的内容来源是「大模型生成的回答」，
 * 而模型回答里可能包含知识库文档的原文。也就是说，如果管理员上传的
 * 文档里藏了 <script> 标签，模型有可能把它原样带进回答里。
 * 所以渲染出来的 HTML **必须**经过 DOMPurify 消毒才能塞进 v-html，
 * 否则就是一个存储型 XSS 漏洞。
 */
import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/core'
import bash from 'highlight.js/lib/languages/bash'
import css from 'highlight.js/lib/languages/css'
import javascript from 'highlight.js/lib/languages/javascript'
import json from 'highlight.js/lib/languages/json'
import python from 'highlight.js/lib/languages/python'
import sql from 'highlight.js/lib/languages/sql'
import xml from 'highlight.js/lib/languages/xml'
import { marked } from 'marked'
import { computed } from 'vue'

import 'highlight.js/styles/github.css'

// 按需注册语言，而不是 import 整个 highlight.js。
// highlight.js 默认会打包 190 多种语言的语法定义，整个包接近 1MB；
// 而商品问答场景里出现代码块的频率很低，只需要覆盖常见几种就够了。
// 实测这样能把聊天页面的打包体积从 1MB 降到 300KB 左右。
const LANGUAGES = { bash, css, javascript, json, python, sql, xml }
Object.entries(LANGUAGES).forEach(([name, lang]) => hljs.registerLanguage(name, lang))

const props = defineProps({
  content: { type: String, default: '' },
  /** 这一条消息可用的引用编号，用来判断角标是否有效 */
  citationCount: { type: Number, default: 0 },
})

const emit = defineEmits(['citation-click'])

// marked 配置：开启 GFM（表格、删除线等）和换行转 <br>
marked.setOptions({
  gfm: true,
  breaks: true,
})

/**
 * 把回答里的 [1] 这种标注变成可点击的角标。
 *
 * 为什么在 Markdown 解析**之前**做替换：解析之后再处理的话，
 * 得去 HTML 里找文本节点，容易误伤标签属性。
 * 替换成一段行内 HTML，marked 会原样保留它。
 *
 * 正则里那个 (?!\() 是必须的：Markdown 的链接语法长这样 [文字](网址)，
 * 如果不排除后面跟括号的情况，形如 [1](http://...) 的链接会被拆坏。
 */
function injectCitationBadges(markdown) {
  return markdown.replace(/\[(\d{1,2})\](?!\()/g, (match, num) => {
    const index = Number(num)
    // 超出范围的编号不做成角标（模型偶尔会瞎标一个不存在的号），
    // 保留原样文字，让用户能看出模型标了个无效引用
    if (props.citationCount > 0 && index > props.citationCount) {
      return match
    }
    return `<sup class="cite-badge" data-cite="${index}">[${index}]</sup>`
  })
}

/** 给代码块加上语法高亮 */
function highlightCode(html) {
  if (typeof document === 'undefined') return html

  const container = document.createElement('div')
  container.innerHTML = html

  container.querySelectorAll('pre code').forEach((block) => {
    try {
      hljs.highlightElement(block)
    } catch {
      // 高亮失败就保持原样，不影响阅读
    }
  })

  return container.innerHTML
}

const renderedHtml = computed(() => {
  const source = props.content || ''
  if (!source.trim()) return ''

  try {
    const withBadges = injectCitationBadges(source)
    let html = marked.parse(withBadges)

    // 消毒：只允许安全的标签和属性。
    // data-cite 是我们自己加的，要显式加进白名单，否则会被过滤掉
    html = DOMPurify.sanitize(html, {
      ADD_ATTR: ['data-cite', 'target', 'rel'],
      // 禁止这些危险标签，双保险
      FORBID_TAGS: ['script', 'style', 'iframe', 'form', 'input'],
      FORBID_ATTR: ['onerror', 'onload', 'onclick'],
    })

    return highlightCode(html)
  } catch (e) {
    // Markdown 格式千奇百怪，解析失败就退回纯文本展示，不能让整条消息消失
    console.error('Markdown 渲染失败：', e)
    return DOMPurify.sanitize(source.replace(/\n/g, '<br>'))
  }
})

/**
 * 点击角标时把编号告诉父组件。
 *
 * 用事件委托而不是给每个角标绑事件：回答是流式追加的，
 * 每个 token 都会重新渲染，逐个绑事件既麻烦又容易漏。
 */
function handleClick(event) {
  const badge = event.target.closest('.cite-badge')
  if (!badge) return

  const index = Number(badge.dataset.cite)
  if (index) {
    emit('citation-click', index)
  }
}
</script>

<template>
  <div class="markdown-body" v-html="renderedHtml" @click="handleClick" />
</template>

<style scoped>
.markdown-body {
  font-size: 14px;
  line-height: 1.8;
  color: #303133;
  word-break: break-word;
}

/* 让 v-html 渲染出来的内容也能套上样式，所以要穿透 scoped */
.markdown-body :deep(p) {
  margin: 0 0 10px;
}

.markdown-body :deep(p:last-child) {
  margin-bottom: 0;
}

.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3),
.markdown-body :deep(h4) {
  margin: 14px 0 8px;
  font-size: 15px;
  font-weight: 600;
}

.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  margin: 6px 0 10px;
  padding-left: 22px;
}

.markdown-body :deep(li) {
  margin: 3px 0;
}

.markdown-body :deep(code) {
  padding: 2px 5px;
  font-family: Consolas, Monaco, monospace;
  font-size: 13px;
  background: #f5f7fa;
  border-radius: 3px;
}

.markdown-body :deep(pre) {
  margin: 10px 0;
  padding: 12px;
  background: #f6f8fa;
  border-radius: 6px;
  overflow-x: auto;
}

.markdown-body :deep(pre code) {
  padding: 0;
  background: none;
}

.markdown-body :deep(table) {
  width: 100%;
  margin: 10px 0;
  border-collapse: collapse;
  font-size: 13px;
}

.markdown-body :deep(th),
.markdown-body :deep(td) {
  padding: 7px 10px;
  border: 1px solid #ebeef5;
  text-align: left;
}

.markdown-body :deep(th) {
  background: #fafafa;
  font-weight: 600;
}

.markdown-body :deep(blockquote) {
  margin: 10px 0;
  padding: 6px 12px;
  border-left: 3px solid #dcdfe6;
  background: #fafafa;
  color: #606266;
}

.markdown-body :deep(a) {
  color: #409eff;
}

/* 引用角标：可点击，点了会打开右侧对应的来源 */
.markdown-body :deep(.cite-badge) {
  display: inline-block;
  margin: 0 2px;
  padding: 0 5px;
  font-size: 11px;
  line-height: 17px;
  font-weight: 600;
  color: #409eff;
  background: #ecf5ff;
  border: 1px solid #b3d8ff;
  border-radius: 9px;
  cursor: pointer;
  vertical-align: super;
  transition: all 0.15s;
}

.markdown-body :deep(.cite-badge:hover) {
  color: #fff;
  background: #409eff;
  border-color: #409eff;
}
</style>
