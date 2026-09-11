/**
 * Cesium 预览的量测:点(含高程)、线(距离)、面(面积)。
 *
 * 结果只放在内存里(items),不入库——预览页是独立入口、不接 Pinia,量测读数
 * 看完即弃,存下来也无从复现当时的视角。
 *
 * 取点用 globe.pick(相机射线与地形求交)而非 scene.pickPosition:后者依赖深度
 * 纹理,关闭地形或在瓦片未加载的区域会返回不可靠的值。
 */
import { ref } from 'vue'
import {
  Cartesian3, Cartographic, Color, EllipsoidGeodesic, Math as CesiumMath,
  ScreenSpaceEventHandler, ScreenSpaceEventType, CallbackProperty,
  HeightReference, LabelStyle, VerticalOrigin, HorizontalOrigin, Cartesian2,
  SceneTransforms,
  sampleTerrainMostDetailed,
} from 'cesium'
import {
  toCgcs2000, fmtLonLatText, fmtPlaneText, fmtCentralMeridianText,
} from '../utils/crs'
import { fmtLen, fmtArea } from '../utils/format'
import { cullLabelBoxes, MAX_OVERLAP } from '../utils/labelCull'

/** WGS84 等面积半径:球面多边形面积公式用它,在本工具的范围尺度上误差可忽略 */
const AUTHALIC_R = 6371007.18

/** 球面多边形面积(球面剩余量法)。carto 为弧度制经纬度序列 */
function sphericalArea(carto) {
  const n = carto.length
  if (n < 3) return 0
  let sum = 0
  for (let i = 0; i < n; i++) {
    const a = carto[i]
    const b = carto[(i + 1) % n]
    let dLon = b.longitude - a.longitude
    // 跨 ±180° 时经差要归一化,否则会算出一个绕地球的巨大面积
    if (dLon > Math.PI) dLon -= 2 * Math.PI
    if (dLon < -Math.PI) dLon += 2 * Math.PI
    sum += dLon * (2 + Math.sin(a.latitude) + Math.sin(b.latitude))
  }
  return Math.abs(sum * AUTHALIC_R * AUTHALIC_R / 2)
}

/**
 * 面标注的锚点:多边形内部一点(思路同 PostGIS 的 ST_PointOnSurface)。
 * 不能用质心/顶点均值——半环形、月牙形、L 形的质心都会落在面外面,标注就飘到
 * 图形之外了。做法是横扫若干条纬线,求与各边的交点;交点排序后按奇偶配对得到
 * 位于面内的区间,取其中最宽一段的中点。
 *
 * carto 为弧度制经纬度序列(不含重复的闭合点)。跨 ±180° 的面不在本工具的
 * 使用范围内,故不做经度归一化。
 */
function interiorPoint(carto) {
  const lats = carto.map((c) => c.latitude)
  const minLat = Math.min(...lats)
  const maxLat = Math.max(...lats)
  let best = null
  const SCANS = 21
  for (let k = 1; k < SCANS; k++) {
    const y = minLat + ((maxLat - minLat) * k) / SCANS
    const xs = []
    for (let i = 0; i < carto.length; i++) {
      const a = carto[i]
      const b = carto[(i + 1) % carto.length]
      // 只取真正跨越扫描线的边,顶点正好落在线上时不重复计数
      if ((a.latitude <= y) === (b.latitude <= y)) continue
      const t = (y - a.latitude) / (b.latitude - a.latitude)
      xs.push(a.longitude + t * (b.longitude - a.longitude))
    }
    xs.sort((p, q) => p - q)
    for (let i = 0; i + 1 < xs.length; i += 2) {
      const w = xs[i + 1] - xs[i]
      if (!best || w > best.w) {
        best = { w, longitude: (xs[i] + xs[i + 1]) / 2, latitude: y }
      }
    }
  }
  if (best) return best
  // 退化图形(所有点共线等)扫不出内部区间,回落顶点均值
  return {
    longitude: carto.reduce((s, c) => s + c.longitude, 0) / carto.length,
    latitude: lats.reduce((s, v) => s + v, 0) / carto.length,
  }
}

/** 沿椭球面的折线长度(逐段大地线,不含高差) */
function geodesicLength(carto) {
  let total = 0
  const g = new EllipsoidGeodesic()
  for (let i = 1; i < carto.length; i++) {
    g.setEndPoints(carto[i - 1], carto[i])
    total += g.surfaceDistance
  }
  return total
}

/** 各拐点的读数:本段长度(与上一点)+ 到该点的累计长度 */
function segmentLabels(carto) {
  const out = []
  const g = new EllipsoidGeodesic()
  let total = 0
  for (let i = 1; i < carto.length; i++) {
    g.setEndPoints(carto[i - 1], carto[i])
    total += g.surfaceDistance
    out.push({ index: i, text: `本段 ${fmtLen(g.surfaceDistance)}\n累计 ${fmtLen(total)}`, total })
  }
  return out
}

/** 三种量测各用一色,与 2D 地图量测保持一致的配色 */
const COLOR = {
  point: { main: '#2563eb', label: 'rgba(37,99,235,0.92)' },
  line: { main: '#16a34a', label: 'rgba(22,163,74,0.92)' },
  area: { main: '#ea580c', label: 'rgba(234,88,12,0.92)' },
}

const LABEL_FONT = '15px Consolas, monospace'
const LABEL_PAD = new Cartesian2(6, 4)

/**
 * 量算标注框宽度(像素)。用离屏 canvas 实测最长那一行,而不是按字数估:
 * 读数里既有数字也有中文,字宽差一倍。
 */
let measureCtx = null
function labelBoxWidth(text) {
  if (!measureCtx) {
    measureCtx = document.createElement('canvas').getContext('2d')
    measureCtx.font = LABEL_FONT
  }
  let max = 0
  for (const line of String(text).split('\n')) {
    max = Math.max(max, measureCtx.measureText(line).width)
  }
  return max + LABEL_PAD.x * 2
}

/**
 * 15px 字体单行行高的估算值(px)。Cesium Label 是 WebGL 渲染,拿不到 DOM 尺寸,
 * 只能按字体估。碰撞判定有 20% 容差,不必像素级精确。
 */
const LABEL_LINE_HEIGHT = 20

/** 标注背景框的完整像素尺寸(含 padding):宽取最长行,高按行数累加 */
function labelBoxSize(text) {
  const w = labelBoxWidth(text)
  const h = String(text).split('\n').length * LABEL_LINE_HEIGHT + LABEL_PAD.y * 2
  return { w, h }
}

/**
 * Cesium 的 Label 在 horizontalOrigin=LEFT 下,每一行都从同一个左边界起排
 * (文字内部左对齐,这是我们要的);但整个背景框会从锚点向右展开,看起来是
 * "标注挂在点的右边"。把 pixelOffset.x 左移半个框宽,框就以点为中心,
 * 同时保留内部左对齐。用 CENTER origin 做不到这点——那样每行会各自居中,
 * 多行读数长短不一就会排得参差。
 */
function labelGraphics(text, type = 'line', clampToGround = false) {
  return {
    text,
    font: LABEL_FONT,
    fillColor: Color.WHITE,
    showBackground: true,
    backgroundColor: Color.fromCssColorString(COLOR[type].label),
    backgroundPadding: LABEL_PAD,
    style: LabelStyle.FILL,
    verticalOrigin: VerticalOrigin.BOTTOM,
    horizontalOrigin: HorizontalOrigin.LEFT,
    pixelOffset: new Cartesian2(-labelBoxWidth(text) / 2, -14),
    // 线/面的读数标注贴地:锚点高度若用拾取值,地形细化后标注就会悬空或陷入地面。
    // 点量测例外——那里要显示的正是精采出来的绝对高程,必须用 NONE。
    heightReference: clampToGround
      ? HeightReference.CLAMP_TO_GROUND : HeightReference.NONE,
    disableDepthTestDistance: Number.POSITIVE_INFINITY,
  }
}

/**
 * 动态标注(绘制过程中文本会变)的偏移也得跟着文本变宽变窄,
 * 否则框会随读数变长而偏出中心。
 */
function dynamicLabelOffset(textProvider) {
  return new CallbackProperty(
    () => new Cartesian2(-labelBoxWidth(textProvider()) / 2, -14), false)
}

/**
 * 拐点圆点:白心 + 类型色描边。
 * 贴地(CLAMP_TO_GROUND)而非用拾取时的绝对高度:拾取高度取自当次的地形
 * LOD,缩放后地形细化,固定高度的点就会陷进地里或浮在半空。贴地也与
 * clampToGround 的线/面轮廓保持一致。
 */
function vertexPoint(type) {
  return {
    pixelSize: 8,
    color: Color.WHITE,
    outlineColor: Color.fromCssColorString(COLOR[type].main),
    outlineWidth: 2,
    heightReference: HeightReference.CLAMP_TO_GROUND,
    disableDepthTestDistance: Number.POSITIVE_INFINITY,
  }
}

export function createCesiumMeasure(viewer) {
  const items = ref([])
  const mode = ref(null)

  let handler = null
  let pending = []          // 已确认的点(Cartesian3)
  let floating = null       // 跟随鼠标的临时点
  let sketchEntities = []   // 绘制过程中的临时 entity
  let pendingLabels = []    // 绘制过程中每个已确认拐点的即时标注 entity
  let cursorEntity = null   // 跟随鼠标的光标点(进入量测就显示)
  let cursorPos = null
  const owned = new Map()   // id → entity 数组
  let seq = 0
  let pendingFrame = 0

  /**
   * 参与碰撞判定的标注,按优先级从高到低:
   * 1) 绘制中的动态读数(跟随鼠标)——当前正在操作的焦点
   * 2) 绘制中已确认的拐点标注——越靠后越新
   * 3) 已完成的量测——items 新在前;同一条线内部按拐点倒序
   */
  function collectLabelEntities() {
    const out = []
    for (const e of sketchEntities) if (e.label) out.push(e)
    for (let i = pendingLabels.length - 1; i >= 0; i--) out.push(pendingLabels[i])
    for (const it of items.value) {
      const ents = owned.get(it.id)
      if (!ents) continue
      const labels = ents.filter((e) => e.label)
      for (let i = labels.length - 1; i >= 0; i--) out.push(labels[i])
    }
    return out
  }

  /**
   * 贴地标注(CLAMP_TO_GROUND)的实际渲染位置钉在地形表面,而 entity.position 里
   * 存的是拾取高度或椭球面 0 高。投影屏幕时按地形高度修正,否则起伏大的区域
   * 标注框会飘在真实显示位置之外,碰撞判定失真。globe.getHeight 同步返回当前
   * 瓦片插值高度,拿不到(瓦片未加载)就回落原位置。
   */
  function projectToScreen(e, time) {
    const scene = viewer.scene
    const pos = e.position.getValue(time)
    if (!pos) return null
    let anchor = pos
    if (e.label.heightReference === HeightReference.CLAMP_TO_GROUND) {
      const c = Cartographic.fromCartesian(pos)
      const h = scene.globe.getHeight(c)
      if (h !== undefined) {
        anchor = Cartesian3.fromRadians(c.longitude, c.latitude, h)
      }
    }
    return SceneTransforms.worldToWindowCoordinates(scene, anchor)
  }

  /**
   * 标注碰撞剔除:与 2D 同一套算法(utils/labelCull),屏幕空间 AABB 相交,
   * 重叠超过 20% 的让位隐藏。全量重算,不依赖增量状态。
   *
   * 框的几何关系与 labelGraphics 的定位参数必须一致:
   * horizontalOrigin=LEFT、verticalOrigin=BOTTOM,背景框在文本外扩 padding,
   * 锚点像素坐标再叠加 pixelOffset(动态标注的偏移会随文本变宽变窄)。
   */
  function relayout() {
    if (viewer.isDestroyed()) return
    const canvas = viewer.scene.canvas
    const viewport = [canvas.clientWidth, canvas.clientHeight]
    if (!viewport[0] || !viewport[1]) return
    const time = viewer.clock.currentTime
    const boxes = []
    for (const e of collectLabelEntities()) {
      const text = e.label.text.getValue(time)
      if (!text) continue
      const sp = projectToScreen(e, time)
      if (!sp) continue          // 相机背面:视口外,不参与判定
      const po = e.label.pixelOffset.getValue(time)
      const cx = sp.x + po.x
      const cy = sp.y + po.y
      const { w, h } = labelBoxSize(text)
      boxes.push({
        el: e,
        left: cx - LABEL_PAD.x,
        top: cy - h + LABEL_PAD.y,
        right: cx - LABEL_PAD.x + w,
        bottom: cy + LABEL_PAD.y,
        area: w * h,
      })
    }
    const hidden = cullLabelBoxes(boxes, viewport, MAX_OVERLAP)
    boxes.forEach((b, i) => { b.el.label.show = !hidden[i] })
  }

  /** 同一帧内多次触发只重算一次(相机移动、鼠标移动都高频) */
  function scheduleRelayout() {
    if (pendingFrame) return
    pendingFrame = requestAnimationFrame(() => {
      pendingFrame = 0
      relayout()
    })
  }

  // 相机一动标注在屏幕上的位置就变,碰撞关系随之变化。moveEnd 覆盖动画结束,
  // changed 覆盖持续缩放/平移过程(两者都走 rAF 节流)。
  viewer.camera.moveEnd.addEventListener(scheduleRelayout)
  viewer.camera.changed.addEventListener(scheduleRelayout)

  function pick(windowPos) {
    const ray = viewer.camera.getPickRay(windowPos)
    if (!ray) return null
    return viewer.scene.globe.pick(ray, viewer.scene) || null
  }

  function toCarto(list) {
    return list.map((c) => Cartographic.fromCartesian(c))
  }

  function clearSketch() {
    for (const e of sketchEntities) viewer.entities.remove(e)
    sketchEntities = []
    for (const l of pendingLabels) viewer.entities.remove(l)
    pendingLabels = []
    pending = []
    floating = null
    scheduleRelayout()
  }

  /**
   * 跟随鼠标的光标点。2D 的 Draw 交互自带这个反馈(sketchPoint),Cesium 没有,
   * 得自己加一个:量测时能看出当前会落在哪儿,也提示"现在处于绘制状态"。
   * 位置走 CallbackProperty,鼠标移动只改 cursorPos,不重建 entity。
   */
  function addCursor(type) {
    cursorPos = null
    cursorEntity = viewer.entities.add({
      position: new CallbackProperty(() => cursorPos || undefined, false),
      point: {
        ...vertexPoint(type),
        // 比已确认的拐点小一号,避免误认为已经落点
        pixelSize: 7,
        color: Color.fromCssColorString(COLOR[type].main).withAlpha(0.65),
      },
    })
  }

  function removeCursor() {
    if (cursorEntity) { viewer.entities.remove(cursorEntity); cursorEntity = null }
    cursorPos = null
  }

  /** 当前(含跟随点)的顶点序列,供动态标注读数 */
  function livePoints() {
    return floating ? [...pending, floating] : [...pending]
  }

  function addSketch(type) {
    const main = Color.fromCssColorString(COLOR[type].main)
    const positions = new CallbackProperty(() => {
      const pts = livePoints()
      // 面需要闭合环才能看出形状
      return type === 'area' && pts.length > 2 ? [...pts, pts[0]] : pts
    }, false)
    sketchEntities.push(viewer.entities.add({
      polyline: { positions, width: 2, clampToGround: true, material: main },
    }))
    if (type === 'area') {
      sketchEntities.push(viewer.entities.add({
        polygon: {
          hierarchy: new CallbackProperty(() => {
            const pts = livePoints()
            return pts.length >= 3 ? { positions: pts } : undefined
          }, false),
          material: main.withAlpha(0.15),
        },
      }))
    }
    const sketchText = () => {
      const carto = toCarto(livePoints())
      if (type === 'line') {
        if (carto.length < 2) return '点击起点'
        const labels = segmentLabels(carto)
        const last = labels[labels.length - 1]
        return last ? last.text : ''
      }
      // 面只报面积,不报距离:量面时的分段长度是噪声
      return carto.length >= 3 ? fmtArea(sphericalArea(carto)) : '继续点击顶点'
    }
    sketchEntities.push(viewer.entities.add({
      position: new CallbackProperty(() => livePoints().slice(-1)[0], false),
      label: {
        ...labelGraphics('', type),
        text: new CallbackProperty(sketchText, false),
        // 文本随绘制变化,偏移量也要跟着重算才能一直居中
        pixelOffset: dynamicLabelOffset(sketchText),
      },
    }))
    scheduleRelayout()
  }

  /** 点量测:高程取自地形。有地形时再精采一次,否则读的是当前瓦片层级的粗值 */
  async function finishPoint(pos) {
    const carto = Cartographic.fromCartesian(pos)
    let height = carto.height
    let sampled = false
    try {
      const r = await sampleTerrainMostDetailed(
        viewer.terrainProvider, [Cartographic.clone(carto)])
      if (r?.[0] && Number.isFinite(r[0].height)) { height = r[0].height; sampled = true }
    } catch (_) { /* 无地形或采样失败,用射线交点的高程 */ }

    const lon = CesiumMath.toDegrees(carto.longitude)
    const lat = CesiumMath.toDegrees(carto.latitude)
    const id = `m${++seq}`
    const plane = toCgcs2000(lon, lat)
    // 高程并进两行坐标末尾,中央经线单独一行
    const lonLatText = fmtLonLatText(lon, lat, height)
    const planeText = fmtPlaneText(plane, height)
    const cmText = fmtCentralMeridianText(plane)
    const text = [lonLatText, planeText, cmText].filter(Boolean).join('\n')
    const position = Cartesian3.fromRadians(carto.longitude, carto.latitude, height)
    owned.set(id, [viewer.entities.add({
      position,
      point: {
        pixelSize: 9, color: Color.fromCssColorString(COLOR.point.main),
        outlineColor: Color.WHITE, outlineWidth: 2,
        heightReference: HeightReference.NONE,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      // Cesium Label 原生支持 \n 换行
      label: labelGraphics(text, 'point'),
    })])
    // 最新结果排在最前
    items.value = [{
      id, type: 'point', lon, lat, height, sampled, plane,
      text,
      lonLatText, planeText, cmText,
      position,
    }, ...items.value]
    scheduleRelayout()
  }

  function finishShape(type) {
    const pts = [...pending]
    const need = type === 'line' ? 2 : 3
    if (pts.length < need) { clearSketch(); return }
    const carto = toCarto(pts)
    const id = `m${++seq}`
    const main = Color.fromCssColorString(COLOR[type].main)
    const ents = []
    if (type === 'line') {
      ents.push(viewer.entities.add({
        polyline: { positions: pts, width: 3, clampToGround: true, material: main },
      }))
    } else {
      ents.push(viewer.entities.add({
        polygon: { hierarchy: { positions: pts }, material: main.withAlpha(0.18) },
        polyline: {
          positions: [...pts, pts[0]], width: 2, clampToGround: true, material: main,
        },
      }))
    }
    // 每个拐点补一个圆点,节点位置更清楚
    for (const p of pts) {
      ents.push(viewer.entities.add({ position: p, point: vertexPoint(type) }))
    }
    const value = type === 'line' ? geodesicLength(carto) : sphericalArea(carto)
    const text = type === 'line' ? fmtLen(value) : fmtArea(value)
    let anchor
    if (type === 'line') {
      // 每个拐点一个读数标注:本段 + 累计,贴地跟随地形
      for (const s of segmentLabels(carto)) {
        ents.push(viewer.entities.add({
          position: pts[s.index], label: labelGraphics(s.text, type, true),
        }))
      }
      anchor = pts[pts.length - 1]
    } else {
      // 面标注挂在面内部一点(凹多边形的质心可能落在面外),贴地显示
      const p = interiorPoint(carto)
      anchor = Cartesian3.fromRadians(p.longitude, p.latitude, 0)
      ents.push(viewer.entities.add({
        position: anchor,
        label: labelGraphics(text, type, true),
      }))
    }
    owned.set(id, ents)
    items.value = [{
      id, type, text,
      length: type === 'line' ? value : undefined,
      area: type === 'area' ? value : undefined,
      vertices: pts.length,
      position: anchor,
    }, ...items.value]
    clearSketch()
  }

  function start(type) {
    if (mode.value === type) { stop(); return }
    stop()
    mode.value = type
    // 默认双击会把相机锁到实体上,量测时的双击是"结束绘制",两者会打架
    viewer.screenSpaceEventHandler.removeInputAction(ScreenSpaceEventType.LEFT_DOUBLE_CLICK)
    handler = new ScreenSpaceEventHandler(viewer.scene.canvas)
    addCursor(type)

    let lastClickAt = 0

    handler.setInputAction((e) => {
      const pos = pick(e.position)
      if (!pos) return
      if (type === 'point') { finishPoint(pos); return }
      const now = performance.now()
      // 双击的第二次点击会被随后的 LEFT_DOUBLE_CLICK 收尾,这里直接忽略,
      // 否则结束点会多出一个与前一拐点几乎重合的顶点,标注叠在一起
      if (now - lastClickAt < 400) return
      lastClickAt = now
      if (!sketchEntities.length) addSketch(type)
      pending.push(pos)
      floating = pos
      // 每落一个拐点画一个圆点;线量测再附上"本段/累计"读数(面量测不报距离)。
      // 两者同挂在一个 entity 上,右键回退时一并移除
      const g = { position: pos, point: vertexPoint(type) }
      if (type === 'line') {
        const labels = segmentLabels(toCarto(pending))
        const last = labels[labels.length - 1]
        if (last) g.label = labelGraphics(last.text, type, true)
      }
      pendingLabels.push(viewer.entities.add(g))
      scheduleRelayout()
    }, ScreenSpaceEventType.LEFT_CLICK)

    // 光标点跟随鼠标。线/面还要顺带更新 sketch 的跟随顶点——只能合在这一个
    // action 里:ScreenSpaceEventHandler 同一事件类型只保留最后注册的那个
    handler.setInputAction((e) => {
      const pos = pick(e.endPosition)
      cursorPos = pos
      if (type !== 'point' && pos && pending.length) floating = pos
      // 鼠标移动只改 cursor/sketch 位置,相机没动,碰撞关系仍要重算(高频,已节流)
      scheduleRelayout()
    }, ScreenSpaceEventType.MOUSE_MOVE)

    if (type !== 'point') {
      handler.setInputAction(() => {
        lastClickAt = 0
        floating = null
        // 慢双击时第二次点击会绕过上面的时间闸,结束点可能留下两个几乎重合的拐点;
        // 去掉后一个,否则最终标注会与倒数第二个叠在一起
        if (pending.length >= 2) {
          const a = pending[pending.length - 2]
          const b = pending[pending.length - 1]
          if (Cartesian3.distance(a, b) < 1) {
            pending.pop()
            const lab = pendingLabels.pop()
            if (lab) viewer.entities.remove(lab)
          }
        }
        finishShape(type)
      }, ScreenSpaceEventType.LEFT_DOUBLE_CLICK)
      // 右键:移除上一个拐点(回退一点重画),而非结束
      handler.setInputAction(() => {
        if (!pending.length) return
        pending.pop()
        const lab = pendingLabels.pop()
        if (lab) viewer.entities.remove(lab)
        floating = pending.length ? pending[pending.length - 1] : null
        scheduleRelayout()
      }, ScreenSpaceEventType.RIGHT_CLICK)
    }
  }

  function stop() {
    clearSketch()
    removeCursor()
    if (handler) { handler.destroy(); handler = null }
    mode.value = null
  }

  function removeItem(id) {
    for (const e of owned.get(id) || []) viewer.entities.remove(e)
    owned.delete(id)
    items.value = items.value.filter((x) => x.id !== id)
    // 删掉一条后原先被它挤掉的标注要放出来
    scheduleRelayout()
  }

  function clearAll() {
    stop()
    for (const id of [...owned.keys()]) removeItem(id)
    scheduleRelayout()
  }

  /** 飞到某条量测结果 */
  function locate(id) {
    const it = items.value.find((x) => x.id === id)
    if (!it?.position) return
    const carto = Cartographic.fromCartesian(it.position)
    viewer.camera.flyTo({
      destination: Cartesian3.fromRadians(
        carto.longitude, carto.latitude, Math.max(carto.height + 1500, 2000)),
      duration: 1,
    })
  }

  return { items, mode, start, stop, removeItem, clearAll, locate }
}
