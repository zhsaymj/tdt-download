// 天地图下载处理工具 - 前端逻辑(原生 JS + OpenLayers)

const { Map, View, Feature } = ol;
const { Tile: TileLayer, Vector: VectorLayer } = ol.layer;
const { OSM, XYZ, Vector: VectorSource } = ol.source;
const { fromLonLat, toLonLat, transformExtent } = ol.proj;
const { Draw, Modify, Translate } = ol.interaction;
const { Style, Stroke, Fill, Circle: CircleStyle } = ol.style;

const state = {
  bbox: null,        // [west, south, east, north] 经纬度(EPSG:4326)
  feature: null,     // 当前绘制/导入的要素
  shape: null,       // 'rect' | 'polygon' | 'vector'
  geometry: null,    // geojson 几何(多边形/矢量任务用于裁剪),矩形为 null
  editing: false,
};

// 向 proj4 注册 CGCS2000 相关定义(proj4 默认只认少数 EPSG)。
// CGCS2000 用 GRS80 椭球;3 度带高斯克吕格投影。
function registerProj4Defs() {
  if (typeof proj4 === 'undefined') return;
  // CGCS2000 地理坐标
  proj4.defs('EPSG:4490', '+proj=longlat +ellps=GRS80 +no_defs');
  // 4534–4554:中央经线 75E–135E,坐标不含带号(false_easting=500000)
  for (let i = 0; i <= 20; i++) {
    const cm = 75 + i * 3;
    proj4.defs(`EPSG:${4534 + i}`,
      `+proj=tmerc +lat_0=0 +lon_0=${cm} +k=1 +x_0=500000 +y_0=0 +ellps=GRS80 +units=m +no_defs`);
  }
  // 4513–4533:第 25–45 带,坐标含带号(false_easting=带号*1e6+500000)
  for (let i = 0; i <= 20; i++) {
    const zone = 25 + i;
    const cm = zone * 3;
    proj4.defs(`EPSG:${4513 + i}`,
      `+proj=tmerc +lat_0=0 +lon_0=${cm} +k=1 +x_0=${zone * 1000000 + 500000} +y_0=0 +ellps=GRS80 +units=m +no_defs`);
  }
}

// 输出坐标系下拉:WGS84 + 中国范围 CGCS2000 3 度带(两套 EPSG 编码)
function buildCrsOptions() {
  const sel = document.getElementById('crs');
  const opts = [
    ['EPSG:4326', 'WGS84 经纬度 (EPSG:4326)'],
    ['EPSG:4490', 'CGCS2000 经纬度 (EPSG:4490)'],
  ];
  // 4534–4554:CM 75E–135E(坐标不含带号),按中央经线
  for (let i = 0; i <= 20; i++) {
    const cm = 75 + i * 3;
    opts.push([`EPSG:${4534 + i}`, `CGCS2000 3度带 中央经线${cm}°E (EPSG:${4534 + i})`]);
  }
  // 4513–4533:zone 25–45(坐标含带号前缀)
  for (let i = 0; i <= 20; i++) {
    const zone = 25 + i;
    opts.push([`EPSG:${4513 + i}`, `CGCS2000 3度带 第${zone}带/含带号 (EPSG:${4513 + i})`]);
  }
  sel.innerHTML = opts
    .map(([v, t]) => `<option value="${v}">${t}</option>`)
    .join('');
}

// ---------- 底图图层 ----------
// 天地图 Web 墨卡托(EPSG:3857)WMTS,与地图视图投影一致。
// t{0-7} 子域轮询;{z}/{x}/{y} 由 OpenLayers XYZ 源填充。
function tiandituLayer(layerType, token) {
  const layerName = layerType.split('_')[0]; // img_w -> img, cia_w -> cia
  return new TileLayer({
    source: new XYZ({
      url:
        `https://t{0-7}.tianditu.gov.cn/${layerType}/wmts?` +
        `SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=${layerName}` +
        `&STYLE=default&TILEMATRIXSET=w&FORMAT=tiles` +
        `&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&tk=${token}`,
      crossOrigin: 'anonymous',
      maxZoom: 18, // 天地图影像最高 18 级
    }),
  });
}

// 备用底图(无密钥或天地图不可用时回落)
const osmLayer = new TileLayer({ source: new OSM(), visible: false });

// ---------- 绘制矢量层 ----------
const vectorSource = new VectorSource();
const vectorLayer = new VectorLayer({
  source: vectorSource,
  style: new Style({
    stroke: new Stroke({ color: '#ffcc00', width: 2 }),
    fill: new Fill({ color: 'rgba(255,204,0,0.15)' }),
    // 显示角点,便于拖拽缩放
    image: new CircleStyle({
      radius: 5,
      fill: new Fill({ color: '#ffcc00' }),
      stroke: new Stroke({ color: '#7c5c00', width: 1 }),
    }),
  }),
});

// ---------- 任务范围预览层(点击任务卡片时显示) ----------
const previewSource = new VectorSource();
const previewLayer = new VectorLayer({
  source: previewSource,
  style: new Style({
    stroke: new Stroke({ color: '#e11d48', width: 2, lineDash: [6, 4] }),
    fill: new Fill({ color: 'rgba(225,29,72,0.10)' }),
  }),
});

const map = new Map({
  target: 'map',
  layers: [osmLayer, vectorLayer, previewLayer],
  // 视图允许放大到 22 级(overzoom 放大观察);瓦片源封顶 18 级,
  // 超过 18 后用 18 级瓦片拉伸显示,不请求无影像的高层级瓦片。
  view: new View({ center: fromLonLat([104.07, 30.67]), zoom: 4, maxZoom: 22 }),
});

// 用天地图影像 + 注记作为默认底图;密钥缺失时回落 OSM
function setupBasemap(token) {
  if (!token) {
    osmLayer.setVisible(true);
    return;
  }
  const img = tiandituLayer('img_w', token);   // 影像
  const cia = tiandituLayer('cia_w', token);   // 影像注记(地名/边界)
  map.getLayers().insertAt(0, img);
  map.getLayers().insertAt(1, cia);
}

// ---------- 绘制 / 编辑 ----------
let drawInteraction = null;
let modifyInteraction = null;   // 拖角点改形
let translateInteraction = null; // 整体平移
let rectSyncing = false;        // 矩形保形时防止 change 事件递归
let rectChangeKey = null;       // 矩形保形 change 监听器句柄(便于回收)
let rectDragStart = null;       // 拖动前 4 个角(3857)

// 从要素几何同步 bbox(经纬度)。矩形/多边形都用几何外接矩形作为下载范围。
function syncBboxFromFeature() {
  if (!state.feature) return;
  const extent = state.feature.getGeometry().getExtent(); // EPSG:3857
  const [minX, minY, maxX, maxY] = transformExtent(
    extent, 'EPSG:3857', 'EPSG:4326'
  );
  state.bbox = [
    Math.min(minX, maxX), Math.min(minY, maxY),
    Math.max(minX, maxX), Math.max(minY, maxY),
  ];
  onBboxChanged();
}

function removeEditInteractions() {
  if (modifyInteraction) { map.removeInteraction(modifyInteraction); modifyInteraction = null; }
  if (translateInteraction) { map.removeInteraction(translateInteraction); translateInteraction = null; }
  if (rectChangeKey) { ol.Observable.unByKey(rectChangeKey); rectChangeKey = null; }
  rectDragStart = null;
  state.editing = false;
  const btn = document.getElementById('btn-edit');
  btn.textContent = '编辑';
  btn.classList.remove('active');
}

const geojsonFmt = new ol.format.GeoJSON();

// 把要素几何导出为 WGS84 geojson 几何(用于后端裁剪)
function featureGeometryWGS84(feature) {
  return geojsonFmt.writeGeometryObject(feature.getGeometry(), {
    featureProjection: 'EPSG:3857',
    dataProjection: 'EPSG:4326',
  });
}

// 裁剪复选框仅在有可裁剪几何(多边形/矢量)时可用
function updateClipVisibility() {
  const wrap = document.getElementById('clip-wrap');
  const clippable = state.shape === 'polygon' || state.shape === 'vector';
  wrap.style.display = clippable ? 'flex' : 'none';
  if (!clippable) document.getElementById('clip').checked = false;
}

// 绑定要素:记录 feature、shape、geometry,启用编辑按钮,几何变化时同步
function bindFeature(feature, shape) {
  state.feature = feature;
  state.shape = shape;
  state.geometry = shape === 'rect' ? null : featureGeometryWGS84(feature);
  syncBboxFromFeature();
  document.getElementById('btn-edit').disabled = false;
  updateClipVisibility();
  feature.getGeometry().on('change', () => {
    if (rectSyncing) return;
    syncBboxFromFeature();
    if (shape !== 'rect') state.geometry = featureGeometryWGS84(feature);
  });
}

function startDrawRect() {
  removeEditInteractions();
  if (drawInteraction) map.removeInteraction(drawInteraction);
  vectorSource.clear();
  state.feature = null;
  drawInteraction = new Draw({
    source: vectorSource,
    type: 'Circle',
    geometryFunction: Draw.createBox(),
  });
  map.addInteraction(drawInteraction);
  drawInteraction.on('drawend', (e) => {
    map.removeInteraction(drawInteraction);
    drawInteraction = null;
    bindFeature(e.feature, 'rect');
  });
}

function startDrawPolygon() {
  removeEditInteractions();
  if (drawInteraction) map.removeInteraction(drawInteraction);
  vectorSource.clear();
  state.feature = null;
  drawInteraction = new Draw({ source: vectorSource, type: 'Polygon' });
  map.addInteraction(drawInteraction);
  drawInteraction.on('drawend', (e) => {
    map.removeInteraction(drawInteraction);
    drawInteraction = null;
    bindFeature(e.feature, 'polygon');
  });
}

// 矩形保形:拖动任一角点时,固定其对角点,把几何重建为规整矩形
function setupRectConstraint(modify) {
  modify.on('modifystart', () => {
    const ring = state.feature.getGeometry().getCoordinates()[0];
    rectDragStart = ring.slice(0, 4).map((c) => c.slice());
  });
  modify.on('modifyend', () => { rectDragStart = null; });

  rectChangeKey = state.feature.getGeometry().on('change', () => {
    if (!state.editing || state.shape !== 'rect' || rectSyncing || !rectDragStart) return;
    const ring = state.feature.getGeometry().getCoordinates()[0];
    // 定位被拖动的角(与拖动前快照比较)
    let idx = -1;
    for (let i = 0; i < 4; i++) {
      if (ring[i][0] !== rectDragStart[i][0] || ring[i][1] !== rectDragStart[i][1]) {
        idx = i; break;
      }
    }
    if (idx === -1) return;
    const moved = ring[idx];
    const opp = rectDragStart[(idx + 2) % 4]; // 对角点保持不动
    const minX = Math.min(moved[0], opp[0]), maxX = Math.max(moved[0], opp[0]);
    const minY = Math.min(moved[1], opp[1]), maxY = Math.max(moved[1], opp[1]);
    const rect = [[
      [minX, minY], [maxX, minY], [maxX, maxY], [minX, maxY], [minX, minY],
    ]];
    rectSyncing = true;
    state.feature.getGeometry().setCoordinates(rect);
    rectSyncing = false;
    rectDragStart = rect[0]; // 更新基准,支持连续拖动
    syncBboxFromFeature();
  });
}

// 开关编辑模式:平移(拖内部) + 改形(拖角点)。多边形支持加点/右键删点。
function toggleEdit() {
  if (!state.feature) return;
  if (state.editing) {
    removeEditInteractions();
    return;
  }

  const modifyOpts = { source: vectorSource };
  // 矩形:禁止在边上插入新顶点,只能拖 4 个角
  if (state.shape === 'rect') {
    modifyOpts.insertVertexCondition = ol.events.condition.never;
  }
  // 多边形:右键删除顶点(保留至少 3 个),用 deleteCondition
  if (state.shape === 'polygon') {
    modifyOpts.deleteCondition = (evt) => {
      // 右键(或按住 Alt 单击)触发删点
      const isDelete = ol.events.condition.altKeyOnly(evt) ||
        (evt.type === 'pointerup' && evt.originalEvent.button === 2);
      if (!isDelete) return false;
      const ring = state.feature.getGeometry().getCoordinates()[0];
      // 环含闭合点,实际顶点数 = ring.length - 1;少于 4 就不允许再删(删后 <3)
      return (ring.length - 1) > 3;
    };
  }
  modifyInteraction = new Modify(modifyOpts);
  translateInteraction = new Translate({
    features: new ol.Collection([state.feature]),
  });
  modifyInteraction.on('modifyend', syncBboxFromFeature);
  translateInteraction.on('translating', syncBboxFromFeature);
  translateInteraction.on('translateend', syncBboxFromFeature);
  map.addInteraction(translateInteraction);
  map.addInteraction(modifyInteraction);

  state.editing = true;
  const btn = document.getElementById('btn-edit');
  btn.textContent = '结束编辑';
  btn.classList.add('active');

  // 矩形:安装保形约束
  if (state.shape === 'rect') setupRectConstraint(modifyInteraction);
}

function clearDraw() {
  removeEditInteractions();
  if (drawInteraction) { map.removeInteraction(drawInteraction); drawInteraction = null; }
  vectorSource.clear();
  state.feature = null;
  state.shape = null;
  state.geometry = null;
  state.bbox = null;
  document.getElementById('btn-edit').disabled = true;
  updateClipVisibility();
  onBboxChanged();
}

// ---------- 矢量导入(shp/geojson/kml → 统一为 WGS84 geojson) ----------

// 判断 geojson 坐标是否落在经纬度合法范围内(粗判是否已是 WGS84)
function looksLikeLonLat(geojson) {
  let ok = true, checked = 0;
  const scan = (c) => {
    if (!ok || checked > 200) return;
    if (typeof c[0] === 'number') {
      checked++;
      if (Math.abs(c[0]) > 180.5 || Math.abs(c[1]) > 90.5) ok = false;
    } else {
      c.forEach(scan);
    }
  };
  const geoms = geojson.type === 'FeatureCollection'
    ? geojson.features.map((f) => f.geometry).filter(Boolean)
    : geojson.type === 'Feature' ? [geojson.geometry] : [geojson];
  geoms.forEach((g) => g && g.coordinates && scan(g.coordinates));
  return ok;
}

// 用 proj4 把 geojson 所有坐标从 srcDef 转到 WGS84(原地修改坐标)
function reprojectGeojson(geojson, srcDef) {
  const tr = proj4(srcDef, 'EPSG:4326');
  const conv = (c) => {
    if (typeof c[0] === 'number') {
      const [x, y] = tr.forward([c[0], c[1]]);
      c[0] = x; c[1] = y;
    } else {
      c.forEach(conv);
    }
  };
  const geoms = geojson.type === 'FeatureCollection'
    ? geojson.features.map((f) => f.geometry).filter(Boolean)
    : geojson.type === 'Feature' ? [geojson.geometry] : [geojson];
  geoms.forEach((g) => g && g.coordinates && conv(g.coordinates));
  return geojson;
}

async function importVectorFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  try {
    let geojson = null;
    let prjText = null;   // .prj / WKT 文本(用于自动识别源坐标系)
    const byExt = (ext) => files.find((f) => f.name.toLowerCase().endsWith(ext));

    const zipFile = byExt('.zip');
    const shpFile = byExt('.shp');
    const kmlFile = byExt('.kml');
    const gjFile = byExt('.geojson') || byExt('.json');
    const prjFile = byExt('.prj');
    if (prjFile) prjText = await prjFile.text();

    if (zipFile) {
      // shpjs 解 zip:若内含 .prj 会自动重投影到 WGS84
      geojson = await window.shp(await zipFile.arrayBuffer());
    } else if (shpFile) {
      const dbfFile = byExt('.dbf');
      const shpBuf = await shpFile.arrayBuffer();
      const dbfBuf = dbfFile ? await dbfFile.arrayBuffer() : undefined;
      // parseShp 不重投影,输出原始坐标;保留 prjText 供后续自动/手动转换
      const geoms = window.shp.parseShp(shpBuf);
      const recs = dbfBuf ? window.shp.parseDbf(dbfBuf) : [];
      geojson = window.shp.combine([geoms, recs]);
    } else if (gjFile) {
      geojson = JSON.parse(await gjFile.text());
    } else if (kmlFile) {
      const dom = new DOMParser().parseFromString(await kmlFile.text(), 'text/xml');
      geojson = toGeoJSON.kml(dom); // KML 规范即 WGS84
    } else {
      alert('无法识别的矢量文件。支持 GeoJSON / KML / Shapefile(.zip 或 .shp+.dbf+.shx,建议附 .prj)。');
      return;
    }

    // 坐标已是经纬度 → 直接用
    if (looksLikeLonLat(geojson)) {
      loadGeojsonAsRange(geojson);
      return;
    }

    // 非经纬度:先尝试用 .prj 自动转换
    if (prjText) {
      try {
        reprojectGeojson(geojson, prjText);
        if (looksLikeLonLat(geojson)) {
          loadGeojsonAsRange(geojson);
          return;
        }
      } catch (_) { /* 落到手选 */ }
    }

    // 自动识别失败 → 弹窗让用户手选源坐标系
    promptSourceCrs(geojson);
  } catch (e) {
    alert('矢量解析失败:' + (e && e.message ? e.message : e));
  }
}

// 弹出源坐标系选择,确认后用 proj4 转换再加载
function promptSourceCrs(geojson) {
  const modal = document.getElementById('srs-modal');
  const sel = document.getElementById('srs-select');
  document.getElementById('srs-hint').textContent =
    '该矢量坐标不在经纬度范围内,可能是投影坐标。请选择其原始坐标系:';
  // 复用坐标系列表(排除 WGS84 经纬度这个"无需转换"项意义不大,但保留便于选 4490)
  sel.innerHTML = document.getElementById('crs').innerHTML;
  // 默认选一个常见投影带
  sel.value = 'EPSG:4544';
  modal.classList.remove('hidden');

  const cleanup = () => {
    modal.classList.add('hidden');
    document.getElementById('srs-ok').onclick = null;
    document.getElementById('srs-cancel').onclick = null;
  };
  document.getElementById('srs-cancel').onclick = cleanup;
  document.getElementById('srs-ok').onclick = () => {
    const src = sel.value;
    try {
      reprojectGeojson(geojson, src);
      cleanup();
      loadGeojsonAsRange(geojson);
    } catch (e) {
      alert('坐标转换失败:' + (e && e.message ? e.message : e));
    }
  };
}

// 把 geojson(WGS84)加载为下载范围要素
function loadGeojsonAsRange(geojson) {
  const features = geojsonFmt.readFeatures(geojson, {
    dataProjection: 'EPSG:4326',
    featureProjection: 'EPSG:3857',
  });
  if (!features.length) { alert('矢量中没有可用要素。'); return; }

  removeEditInteractions();
  vectorSource.clear();
  // 多要素时用整体外接范围合并显示,几何取全部要素的合集
  let feat;
  if (features.length === 1) {
    feat = features[0];
  } else {
    // 合并为 GeometryCollection 便于裁剪;显示上加入全部要素
    feat = features[0];
  }
  features.forEach((f) => vectorSource.addFeature(f));

  state.feature = feat;
  state.shape = 'vector';
  // 几何:单要素直接取其几何;多要素合成 GeometryCollection
  if (features.length === 1) {
    state.geometry = featureGeometryWGS84(feat);
  } else {
    state.geometry = {
      type: 'GeometryCollection',
      geometries: features.map((f) => featureGeometryWGS84(f)),
    };
  }
  // bbox 取所有要素的外接范围
  const ext = vectorSource.getExtent();
  const [minX, minY, maxX, maxY] = transformExtent(ext, 'EPSG:3857', 'EPSG:4326');
  state.bbox = [minX, minY, maxX, maxY];

  document.getElementById('btn-edit').disabled = false;
  updateClipVisibility();
  onBboxChanged();
  // 缩放到导入范围
  map.getView().fit(ext, { padding: [60, 60, 60, 60], duration: 400, maxZoom: 16 });
}

function onBboxChanged() {
  const b = state.bbox;
  if (!b) {
    document.getElementById('bbox-info').textContent = '尚未选择范围';
    document.getElementById('btn-submit').disabled = true;
    document.getElementById('estimate').textContent = '预计瓦片数:—';
    return;
  }
  document.getElementById('bbox-info').innerHTML =
    `西:${b[0].toFixed(4)} 南:${b[1].toFixed(4)}<br>东:${b[2].toFixed(4)} 北:${b[3].toFixed(4)}`;
  document.getElementById('btn-submit').disabled = false;
  refreshEstimate();
}

// ---------- 预估瓦片数 ----------
async function refreshEstimate() {
  if (!state.bbox) return;
  const [w, s, e, n] = state.bbox;
  const zMin = +document.getElementById('z-min').value;
  const zMax = +document.getElementById('z-max').value;
  try {
    const r = await fetch(
      `/api/tasks/estimate?west=${w}&south=${s}&east=${e}&north=${n}&z_min=${zMin}&z_max=${zMax}`
    );
    const d = await r.json();
    document.getElementById('estimate').textContent = `预计瓦片数:${d.total}`;
  } catch (_) {
    document.getElementById('estimate').textContent = '预计瓦片数:—';
  }
}

// ---------- 提交任务 ----------
async function submitTask() {
  if (!state.bbox) return;
  const zMin = +document.getElementById('z-min').value;
  const zMax = +document.getElementById('z-max').value;
  // 天地图影像最高 18 级,兜底校验(防手动键入超限)
  if (zMin < 1 || zMax < 1 || zMin > 18 || zMax > 18) {
    alert('级别范围必须在 1-18 之间(天地图影像最高 18 级)。');
    return;
  }
  if (zMin > zMax) {
    alert('起始级别不能大于结束级别。');
    return;
  }
  const clipEl = document.getElementById('clip');
  const payload = {
    name: document.getElementById('task-name').value || '影像下载',
    provider: document.getElementById('provider').value,
    bbox: state.bbox,
    z_min: zMin,
    z_max: zMax,
    export: document.getElementById('export').value,
    crs: document.getElementById('crs').value,
    geometry: state.geometry || null,
    clip: !!(clipEl && clipEl.checked && state.geometry),
  };
  const r = await fetch('/api/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!r.ok) {
    const err = await r.json();
    alert('提交失败:' + (err.detail || r.status));
    return;
  }
  await loadTasks();
}

// ---------- 任务列表 ----------
const taskCache = {};

async function loadTasks() {
  const r = await fetch('/api/tasks');
  const tasks = await r.json();
  const box = document.getElementById('task-list');
  box.innerHTML = '';
  tasks.forEach((t) => {
    taskCache[t.id] = t;
    box.appendChild(renderTask(t));
  });
  syncActiveCard();
}

const STATUS_TEXT = {
  pending: '排队中', running: '下载中', done: '已完成',
  failed: '失败', canceled: '已取消', paused: '已暂停',
};

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

// 根据状态生成操作按钮
function taskActions(t) {
  const btns = [];
  if (t.status === 'running' || t.status === 'pending') {
    btns.push(`<button class="mini" data-act="pause" data-id="${t.id}">暂停</button>`);
  }
  if (t.status === 'paused' || t.status === 'failed' || t.status === 'canceled') {
    btns.push(`<button class="mini" data-act="resume" data-id="${t.id}">开始</button>`);
  }
  btns.push(`<button class="mini danger" data-act="delete" data-id="${t.id}">删除</button>`);
  return `<div class="actions">${btns.join('')}</div>`;
}

function renderTask(t) {
  const el = document.createElement('div');
  el.className = 'task';
  el.id = 'task-' + t.id;
  const pct = t.total ? Math.round((t.downloaded / t.total) * 100) : 0;
  el.innerHTML = `
    <span class="status status-${t.status}">${STATUS_TEXT[t.status] || t.status}</span>
    <div class="name">${esc(t.name)}</div>
    <div class="muted">级别 ${t.z_min}-${t.z_max} · ${t.downloaded}/${t.total}${t.failed ? ' · 失败' + t.failed : ''}</div>
    <div class="bar"><i style="width:${pct}%"></i><span class="bar-pct">${pct}%</span></div>
    ${taskActions(t)}`;
  return el;
}

// 任务操作(事件委托)
async function onTaskAction(act, id) {
  if (act === 'delete') {
    if (!confirm('确定删除该任务记录?(不会删除已导出的成果文件)')) return;
    const alsoFiles = confirm('是否同时删除已导出的成果目录及文件?\n\n确定=删除目录和文件\n取消=保留文件,仅删任务记录');
    await fetch(`/api/tasks/${id}?purge=${alsoFiles}`, { method: 'DELETE' });
    delete taskCache[id];
    const el = document.getElementById('task-' + id);
    if (el) el.remove();
    if (detailTaskId === id) closeDetail();  // 删除的正是详情中的任务
    return;
  }
  const r = await fetch(`/api/tasks/${id}/${act}`, { method: 'POST' });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    alert('操作失败:' + (err.detail || r.status));
  }
  await loadTasks();
  if (detailTaskId === id) refreshDetailBody();  // 同步刷新详情,不重新缩放
}

// ---------- 任务详情面板 + 范围预览 ----------
let detailTaskId = null;   // 当前详情面板对应的任务 id

function fmtBbox(b) {
  return `西 ${b[0].toFixed(4)}<br>南 ${b[1].toFixed(4)}<br>东 ${b[2].toFixed(4)}<br>北 ${b[3].toFixed(4)}`;
}

const EXPORT_TEXT = { both: 'GeoTIFF + TMS 瓦片', geotiff: '带坐标 GeoTIFF', tms: 'TMS 瓦片' };
const PROVIDER_TEXT = { tianditu_img: '天地图影像' };

// 用任务 bbox 生成 EPSG:3857 矩形要素画到预览层
function drawPreviewRange() {
  previewSource.clear();
  const t = taskCache[detailTaskId];
  if (!t || !t.bbox) return;
  const [w, s, e, n] = t.bbox;
  const ext = transformExtent([w, s, e, n], 'EPSG:4326', 'EPSG:3857');
  const poly = ol.geom.Polygon.fromExtent(ext);
  previewSource.addFeature(new Feature(poly));
}

function zoomToPreview() {
  const t = taskCache[detailTaskId];
  if (!t || !t.bbox) return;
  const ext = transformExtent(t.bbox, 'EPSG:4326', 'EPSG:3857');
  map.getView().fit(ext, { padding: [60, 60, 60, 60], duration: 400, maxZoom: 16 });
}

// 仅刷新详情面板内容(标题+字段),不动范围与复选框
function refreshDetailBody() {
  const t = taskCache[detailTaskId];
  if (!t) return;
  document.getElementById('detail-title').textContent = t.name || '任务详情';
  document.getElementById('detail-body').innerHTML = `
    <div class="row-kv"><span class="k">状态</span><span class="v status-${t.status}">${STATUS_TEXT[t.status] || t.status}</span></div>
    <div class="row-kv"><span class="k">数据源</span><span class="v">${PROVIDER_TEXT[t.provider] || t.provider}</span></div>
    <div class="row-kv"><span class="k">级别</span><span class="v">${t.z_min} - ${t.z_max}</span></div>
    <div class="row-kv"><span class="k">导出格式</span><span class="v">${EXPORT_TEXT[t.export] || t.export}</span></div>
    <div class="row-kv"><span class="k">瓦片</span><span class="v">${t.downloaded}/${t.total}${t.failed ? ' (失败' + t.failed + ')' : ''}</span></div>
    <div class="row-kv"><span class="k">范围</span><span class="v">${fmtBbox(t.bbox)}</span></div>
    ${t.output_path ? `<div class="row-kv"><span class="k">导出目录</span><span class="v">${esc(t.output_path)}</span></div>` : ''}`;
}

// 根据 detailTaskId 同步卡片选中高亮
function syncActiveCard() {
  document.querySelectorAll('#task-list .task.active')
    .forEach((el) => el.classList.remove('active'));
  if (detailTaskId) {
    const el = document.getElementById('task-' + detailTaskId);
    if (el) el.classList.add('active');
  }
}

function openDetail(id) {
  const t = taskCache[id];
  if (!t) return;
  detailTaskId = id;
  syncActiveCard();
  refreshDetailBody();
  document.getElementById('detail-panel').classList.remove('hidden');
  // 切换/打开任务:遵循复选框当前状态显示范围
  if (document.getElementById('detail-show-range').checked) drawPreviewRange();
  else previewSource.clear();
  // 默认缩放到该任务范围
  zoomToPreview();
}

function closeDetail() {
  detailTaskId = null;
  syncActiveCard();
  previewSource.clear();
  document.getElementById('detail-panel').classList.add('hidden');
}

function updateTaskEl(id) {
  const t = taskCache[id];
  if (!t) return;
  const newEl = renderTask(t);
  if (id === detailTaskId) newEl.classList.add('active');  // 保持选中高亮
  const old = document.getElementById('task-' + id);
  if (old) old.replaceWith(newEl);
  else document.getElementById('task-list').prepend(newEl);
}

// ---------- WebSocket 进度 ----------
function connectWs() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/progress`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'ping') return;
    const t = taskCache[msg.id];
    if (!t) { loadTasks(); return; }
    if (msg.type === 'progress') {
      t.downloaded = msg.downloaded;
      t.failed = msg.failed;
      t.total = msg.total;
    } else if (msg.type === 'task') {
      t.status = msg.status;
      if (msg.message) t.message = msg.message;
      if (msg.output_path) t.output_path = msg.output_path;
    }
    updateTaskEl(msg.id);
    // 详情面板正显示该任务时,同步刷新(不重画范围,避免打断查看)
    if (detailTaskId === msg.id) refreshDetailBody();
  };
  ws.onclose = () => setTimeout(connectWs, 2000); // 断线重连
}

// ---------- 地图信息栏(层级 / 比例尺 / 坐标) ----------
// 屏幕 1 像素对应的米数(EPSG:3857 需按纬度做墨卡托修正),据此估算比例尺分母
function updateInfoBar() {
  const view = map.getView();
  const zoom = view.getZoom();
  const resolution = view.getResolution(); // 投影米/像素(EPSG:3857)

  // Web 地图标准比例尺口径:图上 1 单位 : 实地 N 单位。
  // 屏幕像素物理尺寸按 OGC 标准 0.28mm/px(= 0.00028m),
  // scaleDenom = resolution(米/像素) / 0.00028(米/像素)
  const MPU = 0.00028; // 每像素米数(OGC 标准假设)
  const scaleDenom = resolution / MPU;

  document.getElementById('info-zoom').textContent = `层级:${Math.floor(zoom)}`;
  document.getElementById('info-scale').textContent =
    `比例尺:1:${Math.round(scaleDenom).toLocaleString()}`;
}

function setupInfoBar() {
  updateInfoBar();
  map.getView().on('change:resolution', updateInfoBar);
  map.on('moveend', updateInfoBar);
  map.on('pointermove', (evt) => {
    const [lon, lat] = toLonLat(evt.coordinate);
    document.getElementById('info-coord').textContent =
      `经纬度:${lon.toFixed(5)}, ${lat.toFixed(5)}`;
  });
}

// ---------- 初始化 ----------
async function init() {
  const cfg = await (await fetch('/api/config')).json();
  if (!cfg.has_token) {
    document.getElementById('token-warn').classList.remove('hidden');
  }
  setupBasemap(cfg.basemap_token);
  setupInfoBar();
  registerProj4Defs();
  buildCrsOptions();
  document.getElementById('btn-draw-rect').onclick = startDrawRect;
  document.getElementById('btn-draw-poly').onclick = startDrawPolygon;
  document.getElementById('btn-edit').onclick = toggleEdit;
  document.getElementById('btn-clear').onclick = clearDraw;
  // 矢量导入
  document.getElementById('btn-import').onclick = () =>
    document.getElementById('file-vector').click();
  document.getElementById('file-vector').onchange = (e) => {
    importVectorFiles(e.target.files);
    e.target.value = '';  // 允许重复选同一文件
  };
  // 编辑多边形时用右键删点,禁用地图区域的浏览器右键菜单
  document.getElementById('map').addEventListener('contextmenu', (e) => e.preventDefault());
  // 任务卡片:操作按钮走 action;点击卡片其它区域打开详情
  document.getElementById('task-list').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-act]');
    if (btn) { onTaskAction(btn.dataset.act, btn.dataset.id); return; }
    const card = e.target.closest('.task');
    if (card) openDetail(card.id.replace('task-', ''));
  });
  // 详情面板交互
  document.getElementById('detail-close').onclick = closeDetail;
  document.getElementById('detail-show-range').onchange = (e) => {
    if (e.target.checked) drawPreviewRange();
    else previewSource.clear();
  };
  document.getElementById('detail-zoom').onclick = zoomToPreview;
  document.getElementById('btn-submit').onclick = submitTask;
  document.getElementById('z-min').onchange = refreshEstimate;
  document.getElementById('z-max').onchange = refreshEstimate;
  await loadTasks();
  connectWs();
}

init();
