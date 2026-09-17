/**
 * 格式化函数的测试。
 *
 * 用 Node 自带的 test runner（不是 vitest），和 sse.test.js 一样：
 *   npm test
 *
 * 只测纯函数。这套工具链没有 jsdom、也没有 @vue/test-utils，
 * 组件挂载测试跑不起来——所以凡是值得测的逻辑，
 * 都应该抽成 src/utils/ 下的纯函数再测（这条约定和 sse.js 是一致的）。
 */
import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  describeScope,
  formatDuration,
  formatNumber,
  formatTokens,
} from '../src/utils/format.js'

describe('formatNumber', () => {
  it('给大数字加千分位，方便一眼读出量级', () => {
    assert.equal(formatNumber(1234567), '1,234,567')
  })

  it('一千以下是原样输出，不加分隔符', () => {
    assert.equal(formatNumber(999), '999')
  })

  it('0 要显示成 0，不能显示成"—"', () => {
    // 这条容易写错：判断空值的写法如果写成 `if (!value)`，
    // 0 会被当成"没有值"，看板上就会显示一个破折号，
    // 而用户看到的应该是真实的"0 次"
    assert.equal(formatNumber(0), '0')
  })

  it('null 和 undefined 显示成破折号', () => {
    assert.equal(formatNumber(null), '—')
    assert.equal(formatNumber(undefined), '—')
  })
})

describe('formatTokens', () => {
  it('一万以下直接显示', () => {
    assert.equal(formatTokens(9999), '9,999')
  })

  it('超过一万换算成"万"，否则数字太长会把卡片撑爆', () => {
    assert.equal(formatTokens(123456), '12.3 万')
  })

  it('刚好一万也是万', () => {
    assert.equal(formatTokens(10000), '1.0 万')
  })

  it('没有值时不能崩', () => {
    assert.equal(formatTokens(null), '0')
    assert.equal(formatTokens(undefined), '0')
  })
})

describe('formatDuration', () => {
  it('小于一秒用毫秒，一眼能看出"很快"', () => {
    // 看板上要对比的往往是"首字延迟 300 毫秒"和"总耗时 2.4 秒"，
    // 都用秒显示的话前者变成"0.3 秒"，读起来反而绕
    assert.equal(formatDuration(300), '300 ms')
  })

  it('超过一秒换算成秒，保留一位小数', () => {
    assert.equal(formatDuration(2400), '2.4 s')
  })

  it('刚好一千毫秒用秒', () => {
    assert.equal(formatDuration(1000), '1.0 s')
  })

  it('0 显示成破折号', () => {
    // 0 表示"没有样本"（比如从没有过真实生成、全是缓存命中），
    // 显示"—"比"0 ms"更准确——后者会让人以为是"快到不用时间"
    assert.equal(formatDuration(0), '—')
  })

  it('没有值时不能崩', () => {
    assert.equal(formatDuration(null), '—')
    assert.equal(formatDuration(undefined), '—')
  })
})

describe('describeScope', () => {
  it('选了知识库就把名字写出来', () => {
    // 实测踩过：资料在「服装」库里，检索范围却停在「手机数码」，
    // 问什么都答不上来。把名字写在界面上，用户一眼就能看出选错了
    assert.equal(describeScope('服装'), '「服装」')
  })

  it('没选就是全部知识库', () => {
    assert.equal(describeScope(''), '全部知识库')
    assert.equal(describeScope(null), '全部知识库')
    assert.equal(describeScope(undefined), '全部知识库')
  })
})
