/**
 * 地图标注碰撞剔除:在屏幕像素空间做包围盒相交判定,重叠超阈值的标注让位隐藏。
 *
 * 独立成 utils 是因为量测模块整个依赖 ol,而 ol 的包内引用不带扩展名、Node 直接
 * import 解析不了,算法留在那里就没法单测。这里只做纯几何计算,不碰 DOM 和地图。
 */

/** 允许的最大重叠比例:被盖住超过两成就认为读数已经看不清了 */
export const MAX_OVERLAP = 0.2

/**
 * a 被 b 盖住的比例。
 *
 * 分母是 a 自身面积,不是 IoU:两个标注尺寸悬殊时 IoU 会明显偏小——小标注被大
 * 标注糊掉了却判通过。语义就是"这个标注被遮了百分之多少",与对方大小无关。
 */
export function overlapRatio(a, b) {
  const w = Math.min(a.right, b.right) - Math.max(a.left, b.left)
  if (w <= 0) return 0
  const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top)
  if (h <= 0) return 0
  return a.area > 0 ? (w * h) / a.area : 0
}

/**
 * boxes 需按优先级从高到低排好:靠前的保留,靠后的与已保留者冲突则隐藏。
 * 被隐藏的不进 kept,因此不会连带挤掉后面本可显示的标注。
 *
 * @param {{left:number,top:number,right:number,bottom:number,area:number}[]} boxes 屏幕像素包围盒
 * @param {[number,number]} viewport 地图视口像素尺寸
 * @param {number} maxOverlap 允许的最大重叠比例
 * @returns {boolean[]} 与 boxes 等长,true 表示该标注应隐藏
 */
export function cullLabelBoxes(boxes, [vw, vh], maxOverlap = MAX_OVERLAP) {
  const hidden = []
  const kept = []
  for (const box of boxes) {
    // 视口外的既不必隐藏也不参与遮挡判定:否则屏幕外的标注会把视口内的挤掉
    if (box.right <= 0 || box.bottom <= 0 || box.left >= vw || box.top >= vh) {
      hidden.push(false)
      continue
    }
    const clash = kept.some((k) => overlapRatio(box, k) > maxOverlap)
    hidden.push(clash)
    if (!clash) kept.push(box)
  }
  return hidden
}
