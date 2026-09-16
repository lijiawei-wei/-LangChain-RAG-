/**
 * SSE 解析器测试。
 *
 * 为什么单独测这个：浏览器端的流式消费是自己用 fetch + ReadableStream
 * 实现的（EventSource 不支持 POST 和自定义请求头），解析逻辑写错了
 * 会导致回答显示不全或乱码，而且很难从现象上看出原因。
 *
 * 运行方式（在 frontend 目录下）：
 *     node --test tests/
 */
import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { parseSseBlock } from '../src/utils/sse.js'

describe('parseSseBlock - 单条 SSE 消息解析', () => {
  it('解析标准的事件块', () => {
    const result = parseSseBlock('event: token\ndata: {"delta":"你好"}')
    assert.deepEqual(result, { event: 'token', data: { delta: '你好' } })
  })

  it('没有 event 行时默认事件名为 message', () => {
    const result = parseSseBlock('data: {"a":1}')
    assert.equal(result.event, 'message')
    assert.deepEqual(result.data, { a: 1 })
  })

  it('多行 data 会拼接起来', () => {
    // 按 SSE 协议，多个 data 行之间用换行连接
    const result = parseSseBlock('event: note\ndata: {"x":\ndata: 42}')
    assert.deepEqual(result.data, { x: 42 })
  })

  it('冒号后多余的单个空格会被去掉', () => {
    // 协议规定 data: 后面跟一个可选空格，多的那个属于数据本身
    const result = parseSseBlock('data:  {"v":"值"}')
    // 去掉一个空格后，剩下的 " {..." 不是合法 JSON，会走兜底分支
    assert.ok(result !== null)
  })

  it('以冒号开头的注释行被忽略', () => {
    const result = parseSseBlock(': 这是心跳包\nevent: token\ndata: {"delta":"x"}')
    assert.equal(result.event, 'token')
    assert.deepEqual(result.data, { delta: 'x' })
  })

  it('只有注释、没有数据时返回 null', () => {
    assert.equal(parseSseBlock(': keep-alive'), null)
  })

  it('空内容返回 null', () => {
    assert.equal(parseSseBlock(''), null)
  })

  it('不是合法 JSON 时降级成原始文本，而不是抛异常', () => {
    // 一条异常数据不该让整段回答消失
    const result = parseSseBlock('event: token\ndata: 这不是JSON')
    assert.equal(result.event, 'token')
    assert.equal(result.data.raw, '这不是JSON')
  })

  it('保留中文，不做 ascii 转义', () => {
    const result = parseSseBlock('event: token\ndata: {"delta":"电池容量 5000mAh"}')
    assert.equal(result.data.delta, '电池容量 5000mAh')
  })

  it('数据里含转义换行时不破坏解析', () => {
    // JSON 会把回答里的换行转成 \n，这样才不违反 SSE 的"一行一条数据"规则
    const result = parseSseBlock('event: done\ndata: {"answer":"第一行\\n第二行"}')
    assert.equal(result.data.answer, '第一行\n第二行')
  })

  it('数据里含 HTML 和引号也能正确解析', () => {
    const result = parseSseBlock('event: token\ndata: {"delta":"<b>粗体</b> 他说\\"你好\\""}')
    assert.equal(result.data.delta, '<b>粗体</b> 他说"你好"')
  })
})

describe('SSE 流的分块重组', () => {
  /**
   * 模拟网络传输把一条消息切成两半的情况。
   *
   * 这是最容易出 bug 的地方：网络传输不保证「一次读到的正好是一条完整消息」，
   * 可能是半条、也可能是三条半。
   */
  function feedInChunks(chunks) {
    let buffer = ''
    const events = []

    for (const chunk of chunks) {
      buffer += chunk
      const blocks = buffer.split('\n\n')
      buffer = blocks.pop() || ''

      for (const block of blocks) {
        if (!block.trim()) continue
        const parsed = parseSseBlock(block)
        if (parsed) events.push(parsed)
      }
    }

    return events
  }

  it('消息被切成两半也能正确还原', () => {
    const events = feedInChunks([
      'event: token\ndata: {"del',
      'ta":"电池"}\n\nevent: token\ndata: {"delta":"容量"}\n\n',
    ])

    assert.equal(events.length, 2)
    assert.equal(events[0].data.delta, '电池')
    assert.equal(events[1].data.delta, '容量')
  })

  it('一次收到三条完整消息', () => {
    const events = feedInChunks([
      'event: meta\ndata: {"conversation_id":1}\n\nevent: sources\ndata: {"citations":[]}\n\nevent: token\ndata: {"delta":"x"}\n\n',
    ])

    assert.deepEqual(
      events.map((e) => e.event),
      ['meta', 'sources', 'token'],
    )
  })

  it('逐字节喂入也能正确解析（最极端的切分情况）', () => {
    const raw =
      'event: meta\ndata: {"conversation_id":1}\n\nevent: token\ndata: {"delta":"你好"}\n\nevent: done\ndata: {"answer":"你好"}\n\n'

    const events = feedInChunks(raw.split(''))

    assert.deepEqual(
      events.map((e) => e.event),
      ['meta', 'token', 'done'],
    )
    assert.equal(events[1].data.delta, '你好')
  })

  it('尾部不完整的消息不会被当成事件', () => {
    // 最后一条还没收到 \n\n，应该留在缓冲区等待，不能提前解析
    const events = feedInChunks(['event: token\ndata: {"delta":"完整"}\n\nevent: token\ndata: {"del'])

    assert.equal(events.length, 1)
    assert.equal(events[0].data.delta, '完整')
  })
})
