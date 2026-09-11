/**
 * 地图量测:点(坐标)、线(距离)、面(面积)。
 *
 * 结果不落库。量测是看图时的即时动作:量完读数就用掉了,下次打开工具想复现的是
 * "在哪儿量的",而不是一串脱离上下文的数字;要落库得加表、加接口、加清理策略,
 * 换来的价值撑不起这些成本。因此只存在于本模块的 items 里(刷新即清)。
 *
 * 长度/面积用 ol/sphere 的 getLength/getArea:它们按椭球算大地长度,不受地图
 * 投影(3857)的高纬度面积放大影响——直接量投影平面会在北方偏大得离谱。
 */
import { ref } from 'vue'
import VectorLayer from 'ol/layer/Vector'
import VectorSource from 'ol/source/Vector'
import Overlay from 'ol/Overlay'
import { Draw } from 'ol/interaction'
import { primaryAction } from 'ol/events/condition'
import { Style, Stroke, Fill, Circle as CircleStyle } from 'ol/style'
import { getLength, getArea } from 'ol/sphere'
import LineString from 'ol/geom/LineString'
import MultiPoint from 'ol/geom/MultiPoint'
import { toLonLat, transformExtent } from 'ol/proj'
import { unByKey } from 'ol/Observable'
import { DRAW_Z } from './overlays'
import {
  toCgcs2000, fmtLonLatText, fmtPlaneText, fmtCentralMeridianText,
} from '../utils/crs'
import { fmtLen, fmtArea } from '../utils/format'
import { cullLabelBoxes, MAX_OVERLAP } from '../utils/labelCull'

/** 量测图形压在绘制层之上:量的往往就是刚画的范围,被范围填充盖住就没意义了 */
const MEASURE_Z = DRAW_Z + 10

/** 标注元素相对锚点的像素偏移,与 makeOverlay 的 positioning/offset 必须一致 */
const LABEL_OFFSET_Y = -10

/**
 * 标注 DOM 尺寸缓存。尺寸只跟文本有关(CSS 像素固定,不随地图缩放变),
 * 缓存后缩放过程中每帧只做坐标换算,不再读 offsetWidth 触发重排。
 * 文本是中文+数字多行混排,字宽差近一倍,只能实测不能估算。
 */
const labelSizes = new WeakMap()

function labelSize(el) {
  const hit = labelSizes.get(el)
  if (hit && hit.text === el.textContent) return hit
  const size = { text: el.textContent, w: el.offsetWidth, h: el.offsetHeight }
  // 量到 0 说明这一刻还没排版(容器隐藏等),别缓存,否则这个标注永远按零尺寸参与判定
  if (size.w > 0 && size.h > 0) labelSizes.set(el, size)
  return size
}

const OL_TYPE = { point: 'Point', line: 'LineString', area: 'Polygon' }

/** 三种量测各用一色,图上同时存在多条结果时能一眼分清类型(标注同色) */
const COLOR = {
  point: { main: '#2563eb', fill: 'rgba(37,99,235,0.12)', label: 'rgba(37,99,235,.92)' },
  line: { main: '#16a34a', fill: 'rgba(22,163,74,0.12)', label: 'rgba(22,163,74,.92)' },
  area: { main: '#ea580c', fill: 'rgba(234,88,12,0.14)', label: 'rgba(234,88,12,.92)' },
}

/** 顶点取样:线取全部节点,面取外环节点。供拐点圆点样式使用 */
function vertexGeometry(feature) {
  const g = feature.getGeometry()
  const t = g.getType()
  if (t === 'LineString') return new MultiPoint(g.getCoordinates())
  if (t === 'Polygon') return new MultiPoint(g.getCoordinates()[0])
  return null
}

function buildStyles(type) {
  const c = COLOR[type]
  const base = new Style({
    stroke: new Stroke({ color: c.main, width: 2 }),
    fill: new Fill({ color: c.fill }),
    image: new CircleStyle({
      radius: 5,
      fill: new Fill({ color: c.main }),
      stroke: new Stroke({ color: '#fff', width: 1.5 }),
    }),
  })
  if (type === 'point') return [base]
  // 线/面在每个拐点画一个空心圆点:读数标注才能和具体节点对应上
  const vertex = new Style({
    image: new CircleStyle({
      radius: 4,
      fill: new Fill({ color: '#fff' }),
      stroke: new Stroke({ color: c.main, width: 2 }),
    }),
    geometry: vertexGeometry,
  })
  return [base, vertex]
}

const STYLES = {
  point: buildStyles('point'),
  line: buildStyles('line'),
  area: buildStyles('area'),
}

/**
 * 标注元素用内联样式:overlay 的 DOM 是本模块创建的,不该依赖组件的 scoped CSS。
 * white-space:pre —— 点坐标标注要按行显示 4326/平面两套坐标,靠 \n 换行。
 */
function makeLabelElement(text, type) {
  const el = document.createElement('div')
  el.textContent = text
  el.style.cssText = `background:${COLOR[type].label};color:#fff;font-size:14px;`
    + 'padding:2px 7px;border-radius:4px;white-space:pre;pointer-events:none;'
    + 'font-family:Consolas,monospace;box-shadow:0 1px 4px rgba(15,23,42,.25);'
    + 'line-height:1.45;text-align:left'
  return el
}

export function createMeasureTool(map, hooks = {}) {
  // hooks: { beforeStart() } —— 启动量测前让上层取消未完成的范围绘制/编辑,
  // 否则两个 Draw 交互同时在监听点击,一次点击会同时落到两边
  const items = ref([])
  const mode = ref(null)

  const source = new VectorSource()
  // 按 feature 上记录的量测类型取色:同一图层里点/线/面要分色显示
  const layer = new VectorLayer({
    source,
    zIndex: MEASURE_Z,
    style: (f) => STYLES[f.get('mtype')] || STYLES.line,
  })
  map.addLayer(layer)

  const overlays = new Map()    // id → Overlay[](线量测每个拐点一个标注)
  let draw = null
  let sketchOverlay = null      // 绘制过程中的动态读数(复用一个)
  let sketchType = null         // sketchOverlay 当前的配色对应的类型
  let sketchKey = null
  let vertexOverlays = []       // 绘制过程中每个已确认拐点的即时标注(临时)
  let seq = 0
  let pendingFrame = 0

  /**
   * 碰撞剔除的优先级序:靠前的保留、靠后的让位。必须是稳定的确定序,
   * 否则每次重算保下来的标注会跳变闪烁。
   * 1) 绘制中的动态读数——当前正在操作的焦点,永不隐藏
   * 2) 绘制中已确认的拐点——越靠后越新
   * 3) 已完成的量测——items 本身就是新在前;同一条线内部按拐点倒序
   */
  function collectLabels() {
    const out = []
    if (sketchOverlay && sketchOverlay.getPosition()) out.push(sketchOverlay)
    for (let i = vertexOverlays.length - 1; i >= 0; i--) out.push(vertexOverlays[i])
    const seen = new Set()
    for (const it of items.value) {
      const list = overlays.get(it.id)
      if (!list) continue
      seen.add(it.id)
      for (let i = list.length - 1; i >= 0; i--) out.push(list[i])
    }
    // drawend 里标注先落图、items 后更新,补上还没进 items 的那批
    for (const [id, list] of overlays) {
      if (seen.has(id)) continue
      for (let i = list.length - 1; i >= 0; i--) out.push(list[i])
    }
    return out
  }

  /**
   * 标注碰撞剔除:按优先级逐个放行,与已放行标注的重叠超过阈值的隐藏。
   *
   * 隐藏用 visibility 而不是 display:none —— 后者会让 offsetWidth/offsetHeight 归零,
   * 下一轮就再也测不出尺寸;也不用 setPosition(undefined),那是 stopSketch 表达
   * "本次绘制结束"的语义,复用会打架。
   *
   * 全量重算不做增量:状态残留会让标注永久消失。
   */
  function relayout() {
    const size = map.getSize()
    if (!size) return
    const labels = collectLabels()
    // 先全部读(像素换算 + 尺寸),再全部写 visibility,避免读写交错反复触发重排
    const boxes = []
    for (const ov of labels) {
      const el = ov.getElement()
      const pos = ov.getPosition()
      if (!el || !pos) continue
      const px = map.getPixelFromCoordinate(pos)
      if (!px) continue
      const { w, h } = labelSize(el)
      if (w <= 0 || h <= 0) continue
      const left = px[0] - w / 2
      const top = px[1] + LABEL_OFFSET_Y - h
      boxes.push({ el, left, top, right: left + w, bottom: top + h, area: w * h })
    }
    const hidden = cullLabelBoxes(boxes, size, MAX_OVERLAP)
    boxes.forEach((box, i) => {
      box.el.style.visibility = hidden[i] ? 'hidden' : 'visible'
    })
  }

  /** 同一帧内多次请求只重算一次(缩放动画、几何 change 都是高频) */
  function scheduleRelayout() {
    if (pendingFrame) return
    pendingFrame = requestAnimationFrame(() => {
      pendingFrame = 0
      relayout()
    })
  }

  // 绘制中右键:移除上一个拐点(回退一点重画)。只注册一次,
  // 通过模块级 draw/mode 判断当前是否在量测绘制中,避免每次 start 都挂一个监听
  map.on('contextmenu', (evt) => {
    if (!draw || !mode.value || mode.value === 'point') return
    evt.preventDefault()
    draw.removeLastPoint()
  })

  /**
   * 读数标注贴在哪。面用内部点:OL 绘制中的多边形环是 [p1,…,cursor,p1],
   * getLastCoordinate 拿到的是闭合用的首点,标注会一直钉在起点上。
   * 顶点不足时内部点算不出来(NaN),回落末点。
   */
  function labelPosition(geom, type) {
    if (type === 'point') return geom.getCoordinates()
    if (type === 'area') {
      const c = geom.getInteriorPoint()?.getCoordinates()
      if (c && Number.isFinite(c[0]) && Number.isFinite(c[1])) return c.slice(0, 2)
    }
    return geom.getLastCoordinate()
  }

  /** 折线各拐点的读数:本段长度(与上一点)+ 到该点的累计长度 */
  function lineLabels(coords) {
    const out = []
    let total = 0
    for (let i = 1; i < coords.length; i++) {
      const seg = getLength(new LineString([coords[i - 1], coords[i]]))
      total += seg
      out.push({
        position: coords[i],
        text: `本段 ${fmtLen(seg)}\n累计 ${fmtLen(total)}`,
        total,
      })
    }
    return out
  }

  function compute(geom, type) {
    if (type === 'point') {
      const [lon, lat] = toLonLat(geom.getCoordinates())
      const plane = toCgcs2000(lon, lat)
      // 2D 没有高程,不传 height 即不显示;中央经线单独一行
      const lonLatText = fmtLonLatText(lon, lat)
      const planeText = fmtPlaneText(plane)
      const cmText = fmtCentralMeridianText(plane)
      // text 同时用作地图标注:每项各占一行
      return {
        text: [lonLatText, planeText, cmText].filter(Boolean).join('\n'),
        lon, lat, plane, lonLatText, planeText, cmText,
      }
    }
    if (type === 'line') {
      const labels = lineLabels(geom.getCoordinates())
      const length = labels.length ? labels[labels.length - 1].total : 0
      // 列表里只需要总长;拐点上的分段读数由 labels 单独铺到地图
      return { text: fmtLen(length), length, labels }
    }
    const area = getArea(geom)
    return { text: fmtArea(area), area }
  }

  function makeOverlay(text, position, type) {
    const ov = new Overlay({
      element: makeLabelElement(text, type),
      positioning: 'bottom-center',
      offset: [0, -10],
      stopEvent: false,
    })
    ov.setPosition(position)
    map.addOverlay(ov)
    return ov
  }

  /** labels: [{ text, position }],线量测一条会落多个拐点标注 */
  function addLabels(id, labels, type) {
    overlays.set(id, labels.map((l) => makeOverlay(l.text, l.position, type)))
    scheduleRelayout()
  }

  function clearVertexOverlays() {
    for (const ov of vertexOverlays) map.removeOverlay(ov)
    vertexOverlays = []
    scheduleRelayout()
  }

  /**
   * 绘制过程中随几何变化同步拐点标注:每落一个拐点就显示本段/累计,
   * 不必等画完;右键删点后同步移除尾部多余标注。
   * 只对线量测有意义(面量测的读数在结束时的动态游标上)。
   */
  function syncVertexLabels(geom, type) {
    if (type !== 'line') return
    const labels = lineLabels(geom.getCoordinates().slice(0, -1))
    while (vertexOverlays.length < labels.length) {
      const l = labels[vertexOverlays.length]
      vertexOverlays.push(makeOverlay(l.text, l.position, type))
    }
    while (vertexOverlays.length > labels.length) {
      const ov = vertexOverlays.pop()
      map.removeOverlay(ov)
    }
    scheduleRelayout()
  }

  function stopSketch() {
    if (sketchKey) { unByKey(sketchKey); sketchKey = null }
    if (sketchOverlay) {
      sketchOverlay.setPosition(undefined)
      // 隐藏状态不能留给下次绘制:setPosition 只挪位置,visibility 是我们自己写的
      sketchOverlay.getElement().style.visibility = 'visible'
    }
  }

  /** 开始量测。同一模式再点即停止(按钮起到开关作用) */
  function start(type) {
    if (!OL_TYPE[type]) return
    if (mode.value === type) { stop(); return }
    hooks.beforeStart && hooks.beforeStart()
    if (draw) { map.removeInteraction(draw); draw = null }
    mode.value = type
    // condition 限定左键:默认的 noModifierKeys 不看按键,右键按下也会被当作
    // 落点处理——于是右键会先加一个点、再由 contextmenu 删掉它,结果删的是
    // 当前鼠标位置的新点,而不是上一个已确认的拐点
    draw = new Draw({
      source,
      type: OL_TYPE[type],
      style: STYLES[type],
      condition: primaryAction,
    })
    map.addInteraction(draw)

    draw.on('drawstart', (e) => {
      e.feature.set('mtype', type)
      if (type === 'point') return
      const geom = e.feature.getGeometry()
      clearVertexOverlays()
      if (!sketchOverlay || sketchType !== type) {
        if (sketchOverlay) map.removeOverlay(sketchOverlay)
        sketchOverlay = new Overlay({
          element: makeLabelElement('', type),
          positioning: 'bottom-center', offset: [0, -10], stopEvent: false,
        })
        sketchType = type
        map.addOverlay(sketchOverlay)
      }
      sketchKey = geom.on('change', () => {
        syncVertexLabels(geom, type)
        const r = compute(geom, type)
        // 线:游标处跟着显示"本段/累计",与落定后的拐点标注读数一致
        const last = r.labels?.[r.labels.length - 1]
        sketchOverlay.getElement().textContent = last ? last.text : r.text
        sketchOverlay.setPosition(labelPosition(geom, type))
        scheduleRelayout()
      })
      syncVertexLabels(geom, type)
    })

    // 删到只剩一个点时 OL 会放弃本次绘制,这时不会触发 drawend,临时标注要清掉
    draw.on('drawabort', () => {
      stopSketch()
      clearVertexOverlays()
    })

    draw.on('drawend', (e) => {
      stopSketch()
      clearVertexOverlays()
      const geom = e.feature.getGeometry()
      const id = `m${++seq}`
      e.feature.setId(id)
      const { labels, ...r } = compute(geom, type)
      addLabels(id, labels || [{ text: r.text, position: labelPosition(geom, type) }], type)
      // 最新结果排在最前:量完就要看读数,新的一条不该被历史记录推到列表底部
      items.value = [{
        id, type, ...r,
        extent: transformExtent(geom.getExtent(), 'EPSG:3857', 'EPSG:4326'),
      }, ...items.value]
      // 不移除交互:量测天然是连续动作,量一条就要重新点一次按钮很别扭
    })
  }

  function stop() {
    stopSketch()
    clearVertexOverlays()
    if (draw) { map.removeInteraction(draw); draw = null }
    mode.value = null
  }

  function removeItem(id) {
    const f = source.getFeatureById(id)
    if (f) source.removeFeature(f)
    for (const ov of overlays.get(id) || []) map.removeOverlay(ov)
    overlays.delete(id)
    items.value = items.value.filter((x) => x.id !== id)
    // 删掉一条后原先被它挤掉的标注要放出来
    scheduleRelayout()
  }

  function clearAll() {
    stop()
    source.clear()
    for (const list of overlays.values()) {
      for (const ov of list) map.removeOverlay(ov)
    }
    overlays.clear()
    items.value = []
  }

  /** 定位到某条量测结果。点没有范围,按中心点定位 */
  function locate(id) {
    const it = items.value.find((x) => x.id === id)
    if (!it) return
    const view = map.getView()
    if (it.type === 'point') {
      const f = source.getFeatureById(id)
      if (f) view.animate({ center: f.getGeometry().getCoordinates(), zoom: Math.max(view.getZoom(), 15), duration: 300 })
      return
    }
    view.fit(transformExtent(it.extent, 'EPSG:4326', 'EPSG:3857'),
      { padding: [60, 60, 60, 60], duration: 300, maxZoom: 19 })
  }

  return { items, mode, start, stop, removeItem, clearAll, locate, relayout: scheduleRelayout }
}
