// OpenLayers 地图逻辑封装:底图、绘制/编辑、信息栏、范围预览。
// 以工厂函数返回操作 API;通过回调把 bbox/几何变化同步给上层(Pinia)。
import { Map, View, Feature } from 'ol'
import TileLayer from 'ol/layer/Tile'
import VectorLayer from 'ol/layer/Vector'
import XYZ from 'ol/source/XYZ'
import VectorSource from 'ol/source/Vector'
import { fromLonLat, toLonLat, transformExtent } from 'ol/proj'
import { Draw, Modify, Translate } from 'ol/interaction'
import { createBox } from 'ol/interaction/Draw'
import { Style, Stroke, Fill, Circle as CircleStyle } from 'ol/style'
import { fromExtent as polygonFromExtent } from 'ol/geom/Polygon'
import { GeoJSON } from 'ol/format'
import Collection from 'ol/Collection'
import { never } from 'ol/events/condition'
import { unByKey } from 'ol/Observable'
import { DRAW_Z } from './overlays'
import { createMeasureTool } from './measure'
import { basemapTypesFor, basemapZIndexForLevel } from '../utils/basemap'

const geojsonFmt = new GeoJSON()

export function createMapController(target, hooks = {}) {
  // hooks: { onRangeChange({bbox, geometry, shape}), onEditingChange(bool), onInfo({zoom, scale, lon, lat}) }
  const state = { feature: null, shape: null, editing: false }

  // ---- 图层 ----
  // 绘制层与预览层的 zIndex 必须在 DRAW_Z 以上:成果叠加层用 10~89(见
  // composables/overlays.js),不显式压在其上的话,勾一个影像图层就会把用户
  // 画的选区盖掉——而"叠加成果和选区对不对得上"正是要看的东西。
  const vectorSource = new VectorSource()
  const vectorLayer = new VectorLayer({
    source: vectorSource,
    zIndex: DRAW_Z,
    style: new Style({
      stroke: new Stroke({ color: '#ffcc00', width: 2 }),
      fill: new Fill({ color: 'rgba(255,204,0,0.15)' }),
      image: new CircleStyle({
        radius: 5,
        fill: new Fill({ color: '#ffcc00' }),
        stroke: new Stroke({ color: '#7c5c00', width: 1 }),
      }),
    }),
  })
  // 外接框预览(红色虚线)
  const previewSource = new VectorSource()
  const previewLayer = new VectorLayer({
    source: previewSource,
    zIndex: DRAW_Z + 1,
    style: new Style({
      stroke: new Stroke({ color: '#e11d48', width: 2, lineDash: [6, 4] }),
      fill: new Fill({ color: 'rgba(225,29,72,0.10)' }),
    }),
  })
  // 实际绘制多边形预览(蓝色虚线,区别于外接框)
  const previewGeomSource = new VectorSource()
  const previewGeomLayer = new VectorLayer({
    source: previewGeomSource,
    zIndex: DRAW_Z + 2,
    style: new Style({
      stroke: new Stroke({ color: '#2563eb', width: 2, lineDash: [4, 4] }),
      fill: new Fill({ color: 'rgba(37,99,235,0.10)' }),
    }),
  })

  const map = new Map({
    target,
    layers: [vectorLayer, previewLayer, previewGeomLayer],
    view: new View({ center: fromLonLat([104.07, 30.67]), zoom: 4, maxZoom: 22 }),
  })

  function tiandituLayer(layerType, token) {
    const layerName = layerType.split('_')[0]
    return new TileLayer({
      source: new XYZ({
        url:
          `https://t{0-7}.tianditu.gov.cn/${layerType}/wmts?` +
          `SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=${layerName}` +
          `&STYLE=default&TILEMATRIXSET=w&FORMAT=tiles` +
          `&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&tk=${token}`,
        crossOrigin: 'anonymous',
        maxZoom: 18,
      }),
    })
  }

  let baseLayers = []   // 当前底图图层组(供切换时移除)
  let baseToken = null
  let baseKey = 'tianditu_img'
  let baseOpacity = 1
  let baseLevel = 0

  function applyBasemapStyle() {
    const z = basemapZIndexForLevel(baseLevel)
    for (const l of baseLayers) {
      l.setOpacity(baseOpacity)
      l.setZIndex(z)
    }
  }

  function setupBasemap(token) {
    baseToken = token
    if (!token) { applyBasemapStyle(); return }
    setBasemap(baseKey)
  }

  // 按用户选择/下载数据类型切换中间地图底图(底图 + 对应注记)
  function setBasemap(providerKey) {
    baseKey = providerKey || 'tianditu_img'
    if (!baseToken) { applyBasemapStyle(); return }
    const types = basemapTypesFor(baseKey)
    // 移除旧底图组
    baseLayers.forEach((l) => map.removeLayer(l))
    baseLayers = types.map((t) => tiandituLayer(t, baseToken))
    applyBasemapStyle()
    // 插到最底层(矢量/预览层之下)
    baseLayers.forEach((l, i) => map.getLayers().insertAt(i, l))
  }

  // 兼容旧调用名:下载数据源切换时仍能同步底图
  function setOverlayByProvider(providerKey) { setBasemap(providerKey) }

  function setBasemapOpacity(v) {
    baseOpacity = Math.min(1, Math.max(0, Number(v)))
    applyBasemapStyle()
  }

  function setBasemapLevel(level) {
    baseLevel = Math.max(0, Math.min(2, Number(level) || 0))
    applyBasemapStyle()
  }

  // ---- 信息栏 ----
  function emitInfo(coordinate) {
    const view = map.getView()
    const zoom = Math.floor(view.getZoom())
    const scale = Math.round(view.getResolution() / 0.00028)
    let lon, lat
    if (coordinate) { [lon, lat] = toLonLat(coordinate) }
    hooks.onInfo && hooks.onInfo({ zoom, scale, lon, lat })
  }
  map.getView().on('change:resolution', () => emitInfo())
  map.on('moveend', () => emitInfo())
  map.on('pointermove', (evt) => emitInfo(evt.coordinate))
  // 禁用地图右键菜单(多边形编辑右键删点)
  map.getTargetElement()?.addEventListener('contextmenu', (e) => e.preventDefault())

  // ---- bbox / 几何同步 ----
  function featureGeometryWGS84(feature) {
    return geojsonFmt.writeGeometryObject(feature.getGeometry(), {
      featureProjection: 'EPSG:3857', dataProjection: 'EPSG:4326',
    })
  }
  function currentBbox() {
    const ext = state.feature.getGeometry().getExtent()
    const [minX, minY, maxX, maxY] = transformExtent(ext, 'EPSG:3857', 'EPSG:4326')
    return [Math.min(minX, maxX), Math.min(minY, maxY), Math.max(maxX, minX), Math.max(maxY, minY)]
  }
  function pushRange() {
    if (!state.feature) return
    hooks.onRangeChange && hooks.onRangeChange({
      bbox: currentBbox(),
      geometry: state.shape === 'rect' ? null : featureGeometryWGS84(state.feature),
      shape: state.shape,
    })
  }

  // ---- 绘制 / 编辑 ----
  let drawInteraction = null
  let modifyInteraction = null
  let translateInteraction = null
  let rectSyncing = false
  let rectChangeKey = null
  let rectDragStart = null
  let ctxMenuHandler = null   // 多边形编辑时的右键删点处理器

  function setEditing(v) {
    state.editing = v
    hooks.onEditingChange && hooks.onEditingChange(v)
  }

  function removeEditInteractions() {
    if (modifyInteraction) { map.removeInteraction(modifyInteraction); modifyInteraction = null }
    if (translateInteraction) { map.removeInteraction(translateInteraction); translateInteraction = null }
    if (rectChangeKey) { unByKey(rectChangeKey); rectChangeKey = null }
    if (ctxMenuHandler) {
      map.getTargetElement()?.removeEventListener('contextmenu', ctxMenuHandler)
      ctxMenuHandler = null
    }
    rectDragStart = null
    setEditing(false)
  }

  // 右键就近删除多边形顶点(替代 Modify 不可靠的内部顶点跟踪)。
  // 基于鼠标实际点击位置找全局最近顶点,删的就是所选的点,初始顶点也能删。
  // 同时支持 Polygon 与 MultiPolygon(行政区边界多为 MultiPolygon)。
  function deleteNearestVertex(evt) {
    if (!state.feature) return
    const geom = state.feature.getGeometry()
    const type = geom.getType()
    if (type !== 'Polygon' && type !== 'MultiPolygon') return

    const pixel = map.getEventPixel(evt)
    const clickCoord = map.getCoordinateFromPixel(pixel)
    const tol = 15 * map.getView().getResolution()  // 命中阈值:15 像素

    // 统一成 MultiPolygon 坐标结构 [poly][ring][vertex] 处理
    const coords = type === 'Polygon' ? [geom.getCoordinates()] : geom.getCoordinates()

    // 在所有子多边形的所有环上找全局最近顶点
    let bp = -1, br = -1, bi = -1, bestD = Infinity
    coords.forEach((poly, pi) => {
      poly.forEach((ring, ri) => {
        const n = ring.length - 1   // 末点与首点重合
        for (let i = 0; i < n; i++) {
          const d = Math.hypot(ring[i][0] - clickCoord[0], ring[i][1] - clickCoord[1])
          if (d < bestD) { bestD = d; bp = pi; br = ri; bi = i }
        }
      })
    })
    if (bp === -1 || bestD > tol) return

    // 目标环去重计数,保证删后仍 >3 个顶点(有效多边形)
    const targetRing = coords[bp][br]
    if (targetRing.length - 1 <= 3) return

    const newRing = targetRing.slice(0, -1)
    newRing.splice(bi, 1)
    newRing.push(newRing[0].slice())   // 重新闭合
    coords[bp][br] = newRing

    if (type === 'Polygon') geom.setCoordinates(coords[0])
    else geom.setCoordinates(coords)
    pushRange()
  }

  function bindFeature(feature, shape) {
    state.feature = feature
    state.shape = shape
    pushRange()
    feature.getGeometry().on('change', () => {
      if (rectSyncing) return
      pushRange()
    })
  }

  function startDrawRect() {
    measure.stop()
    removeEditInteractions()
    if (drawInteraction) map.removeInteraction(drawInteraction)
    vectorSource.clear()
    state.feature = null
    drawInteraction = new Draw({ source: vectorSource, type: 'Circle', geometryFunction: createBox() })
    map.addInteraction(drawInteraction)
    drawInteraction.on('drawend', (e) => {
      map.removeInteraction(drawInteraction); drawInteraction = null
      bindFeature(e.feature, 'rect')
    })
  }

  function startDrawPolygon() {
    measure.stop()
    removeEditInteractions()
    if (drawInteraction) map.removeInteraction(drawInteraction)
    vectorSource.clear()
    state.feature = null
    drawInteraction = new Draw({ source: vectorSource, type: 'Polygon' })
    map.addInteraction(drawInteraction)
    drawInteraction.on('drawend', (e) => {
      map.removeInteraction(drawInteraction); drawInteraction = null
      bindFeature(e.feature, 'polygon')
    })
  }

  function setupRectConstraint(modify) {
    modify.on('modifystart', () => {
      const ring = state.feature.getGeometry().getCoordinates()[0]
      rectDragStart = ring.slice(0, 4).map((c) => c.slice())
    })
    modify.on('modifyend', () => { rectDragStart = null })
    rectChangeKey = state.feature.getGeometry().on('change', () => {
      if (!state.editing || state.shape !== 'rect' || rectSyncing || !rectDragStart) return
      const ring = state.feature.getGeometry().getCoordinates()[0]
      let idx = -1
      for (let i = 0; i < 4; i++) {
        if (ring[i][0] !== rectDragStart[i][0] || ring[i][1] !== rectDragStart[i][1]) { idx = i; break }
      }
      if (idx === -1) return
      const moved = ring[idx]
      const opp = rectDragStart[(idx + 2) % 4]
      const minX = Math.min(moved[0], opp[0]), maxX = Math.max(moved[0], opp[0])
      const minY = Math.min(moved[1], opp[1]), maxY = Math.max(moved[1], opp[1])
      const rect = [[[minX, minY], [maxX, minY], [maxX, maxY], [minX, maxY], [minX, minY]]]
      rectSyncing = true
      state.feature.getGeometry().setCoordinates(rect)
      rectSyncing = false
      rectDragStart = rect[0]
      pushRange()
    })
  }

  function toggleEdit() {
    if (!state.feature) return
    if (state.editing) { removeEditInteractions(); return }
    measure.stop()
    // vector(导入矢量/行政区)与 polygon 一样支持加点/删点;rect 固定矩形不删点
    const isPolygonLike = state.shape === 'polygon' || state.shape === 'vector'
    const opts = { source: vectorSource }
    if (state.shape === 'rect') opts.insertVertexCondition = never
    // 删点不走 Modify 内部逻辑(其右键跟踪不可靠),下面用自定义右键处理
    if (isPolygonLike) opts.deleteCondition = never
    modifyInteraction = new Modify(opts)
    translateInteraction = new Translate({ features: new Collection([state.feature]) })
    modifyInteraction.on('modifyend', pushRange)
    translateInteraction.on('translating', pushRange)
    translateInteraction.on('translateend', pushRange)
    map.addInteraction(translateInteraction)
    map.addInteraction(modifyInteraction)
    setEditing(true)
    if (state.shape === 'rect') setupRectConstraint(modifyInteraction)
    // 多边形类:注册右键就近删点(Polygon 与 MultiPolygon 均支持)
    const gtype = state.feature.getGeometry().getType()
    if (isPolygonLike && (gtype === 'Polygon' || gtype === 'MultiPolygon')) {
      ctxMenuHandler = (e) => { e.preventDefault(); deleteNearestVertex(e) }
      map.getTargetElement()?.addEventListener('contextmenu', ctxMenuHandler)
    }
  }

  function clearDraw() {
    removeEditInteractions()
    if (drawInteraction) { map.removeInteraction(drawInteraction); drawInteraction = null }
    vectorSource.clear()
    state.feature = null
    state.shape = null
    hooks.onRangeChange && hooks.onRangeChange({ bbox: null, geometry: null, shape: null })
  }

  // ---- 加载 geojson(WGS84)为下载范围 ----
  function loadGeojson(geojson) {
    const features = geojsonFmt.readFeatures(geojson, {
      dataProjection: 'EPSG:4326', featureProjection: 'EPSG:3857',
    })
    if (!features.length) throw new Error('矢量中没有可用要素。')
    removeEditInteractions()
    vectorSource.clear()
    features.forEach((f) => vectorSource.addFeature(f))
    state.feature = features[0]
    state.shape = 'vector'
    const geometry = features.length === 1
      ? featureGeometryWGS84(features[0])
      : { type: 'GeometryCollection', geometries: features.map((f) => featureGeometryWGS84(f)) }
    const ext = vectorSource.getExtent()
    const [minX, minY, maxX, maxY] = transformExtent(ext, 'EPSG:3857', 'EPSG:4326')
    hooks.onRangeChange && hooks.onRangeChange({
      bbox: [minX, minY, maxX, maxY], geometry, shape: 'vector',
    })
    map.getView().fit(ext, { padding: [60, 60, 60, 60], duration: 400, maxZoom: 16 })
  }

  // ---- 任务范围预览 ----
  // bbox: 外接框(红虚线);geometry: 可选,实际绘制的多边形/矢量(蓝虚线)
  function showPreview(bbox, geometry = null) {
    previewSource.clear()
    previewGeomSource.clear()
    if (bbox) {
      const ext = transformExtent(bbox, 'EPSG:4326', 'EPSG:3857')
      previewSource.addFeature(new Feature(polygonFromExtent(ext)))
    }
    if (geometry) {
      try {
        const feats = geojsonFmt.readFeatures(
          geometry.type === 'FeatureCollection' || geometry.type === 'Feature'
            ? geometry
            : { type: 'Feature', geometry, properties: {} },
          { dataProjection: 'EPSG:4326', featureProjection: 'EPSG:3857' },
        )
        feats.forEach((f) => previewGeomSource.addFeature(f))
      } catch (_) { /* 几何异常忽略,只显示外接框 */ }
    }
  }
  function clearPreview() { previewSource.clear(); previewGeomSource.clear() }
  function zoomTo(bbox) {
    if (!bbox) return
    const ext = transformExtent(bbox, 'EPSG:4326', 'EPSG:3857')
    map.getView().fit(ext, { padding: [60, 60, 60, 60], duration: 400, maxZoom: 16 })
  }

  // ---- 量测 ----
  // 与范围绘制/编辑互斥:两个 Draw 同时挂着,一次点击会同时被两边收到。
  const measure = createMeasureTool(map, {
    beforeStart: () => {
      removeEditInteractions()
      if (drawInteraction) { map.removeInteraction(drawInteraction); drawInteraction = null }
    },
  })

  // 标注间距随缩放变化,视图一动就要重算碰撞剔除。挂在 measure 创建之后避免 TDZ。
  // pointermove 不挂:那条太频繁,而且鼠标移动不改变标注之间的相对位置
  map.getView().on('change:resolution', () => measure.relayout())
  map.on('moveend', () => measure.relayout())

  emitInfo()

  return {
    map, setupBasemap, setOverlayByProvider, setBasemap,
    setBasemapOpacity, setBasemapLevel,
    startDrawRect, startDrawPolygon, toggleEdit, clearDraw, loadGeojson,
    showPreview, clearPreview, zoomTo, measure,
    hasFeature: () => !!state.feature,
  }
}
