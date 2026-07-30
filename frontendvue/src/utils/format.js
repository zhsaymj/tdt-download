// 通用格式化工具

// 字节数转人类可读大小(约值)
export function fmtSize(bytes) {
  if (bytes == null || bytes < 0) return '—'
  if (bytes < 1024) return `${bytes} B`
  const kb = bytes / 1024
  if (kb < 1024) return `${kb.toFixed(0)} KB`
  const mb = kb / 1024
  if (mb < 1024) return `${mb.toFixed(mb < 10 ? 1 : 0)} MB`
  return `${(mb / 1024).toFixed(2)} GB`
}

// 整数千分位分隔
export function fmtNum(n) {
  if (n == null) return '0'
  return Number(n).toLocaleString('en-US')
}

// 剩余秒数 → 人类可读(剩 1分30秒 / 剩 2小时5分)
export function fmtEta(sec) {
  if (sec == null || sec < 0) return ''
  const s = Math.round(sec)
  if (s < 1) return '即将完成'
  if (s < 60) return `剩 ${s}秒`
  const m = Math.floor(s / 60)
  const rs = s % 60
  if (m < 60) return rs ? `剩 ${m}分${rs}秒` : `剩 ${m}分`
  const h = Math.floor(m / 60)
  const rm = m % 60
  return rm ? `剩 ${h}时${rm}分` : `剩 ${h}时`
}
