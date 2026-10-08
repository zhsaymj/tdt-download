/**
 * 任务详情面板可拖拽:整个面板(交互控件除外)作把手,Pointer Events + setPointerCapture。
 *
 * 为什么必须 setPointerCapture:拖动时指针会划过地图,不捕获的话地图会抢走
 * pointermove,拖拽中途"断流"(项目里另一处的浮窗踩过这个坑)。
 * 为什么必须 fixed 定位:面板在 .map-main(overflow:hidden) 内,absolute 拖到顶部
 * 会被容器裁剪、整段消失拖不回来;fixed 相对视口才能盖过头部菜单栏。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const src = readFileSync(new URL('./TaskDetail.vue', import.meta.url), 'utf8')

function stripComments(text) {
  return text
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/^[ \t]*\/\/[^\n]*$/gm, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
}

test('★ 整个面板(而非标题栏)是拖拽把手 ★', () => {
  const code = stripComments(src)
  const di = code.indexOf('class="detail"')
  assert.notEqual(di, -1, '找不到面板根元素')
  const detail = code.slice(di, code.indexOf('</div>', di))
  assert.ok(detail.includes('@pointerdown'),
    '面板根元素没有绑定 pointerdown —— 拖不动')
  // 标题栏不应单独再绑:整面板可拖,标题栏绑定会造成重复/误导
  const hi = code.indexOf('class="head"')
  assert.notEqual(hi, -1, '找不到标题栏')
  const head = code.slice(hi, code.indexOf('</div>', hi))
  assert.ok(!head.includes('@pointerdown'),
    '标题栏还单独绑着 pointerdown —— 现在应该整面板可拖')
})

test('★ 点按钮/复选框不启动拖拽(否则关闭按钮失效) ★', () => {
  const code = stripComments(src)
  assert.ok(/closest\('button/.test(code),
    'onDragStart 没有用 closest 排除 button —— 点「✕」会先触发拖拽逻辑,click 失效')
  assert.ok(/closest\('button[^)]*label/.test(code),
    '排除列表里没有 label(复选框)—— 点复选框会误触发拖拽')
})

test('★ 用 setPointerCapture 防止拖拽断流 ★', () => {
  const code = stripComments(src)
  assert.ok(code.includes('setPointerCapture'),
    '没有 setPointerCapture —— 指针划过地图时拖拽会断流')
  assert.ok(code.includes('releasePointerCapture'),
    '没有 releasePointerCapture —— 捕获不放开会卡住其他交互')
})

test('★ 位移用 transform 表达(不动 left/top) ★', () => {
  const code = stripComments(src)
  assert.ok(/translate\(/.test(code),
    '位移应走 transform —— 不改 left 才能保留原有的 --pad-left 避让与过渡')
})

test('拖拽中禁用过渡,否则跟手会拖影', () => {
  const code = stripComments(src)
  assert.ok(/\.detail\.dragging\s*\{[^}]*transition:\s*none/.test(code),
    '拖拽态没有关掉 transition —— 面板会滞后于指针')
})

test('★ 面板固定定位,拖到顶部不被 .map-main 的 overflow 裁剪 ★', () => {
  const code = stripComments(src)
  const m = code.match(/\.detail\s*\{[^}]*position:\s*(fixed|absolute)/)
  assert.notEqual(m, null, '找不到 .detail 的定位方式')
  assert.equal(m[1], 'fixed',
    `面板应为 fixed —— absolute 在 overflow:hidden 的地图容器里拖到顶部会整段消失,实际 ${m[1]}`)
})

test('★ 面板层级在最上(盖过头部菜单栏与常规面板) ★', () => {
  const code = stripComments(src)
  const m = code.match(/\.detail\s*\{[^}]*z-index:\s*(\d+)/)
  assert.notEqual(m, null, '找不到 .detail 的 z-index')
  // 1000:超过 AppTopBar(无 z-index)与 SidePanel/LayerPanel(30);
  // 仍低于 TDesign 弹层(teleport 到 body,1500+)
  assert.ok(Number(m[1]) >= 1000,
    `z-index 应不低于 1000,实际 ${m[1]}`)
})

test('★ 拖出视口太深自动回弹,保证永远拖得回 ★', () => {
  const code = stripComments(src)
  assert.ok(/drag\.value\s*=\s*\{\s*\.\.\.drag\.value,\s*y:/.test(code),
    '没有回弹逻辑 —— 拖到头部出屏后就抓不到把手,拖不回来')
})
