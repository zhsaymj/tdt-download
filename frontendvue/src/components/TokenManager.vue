<script setup>
import { ref, reactive, watch, computed } from 'vue'
import { MessagePlugin, DialogPlugin } from 'tdesign-vue-next'
import { fmtNum } from '../utils/format'
import { api } from '../api'

const props = defineProps({
  visible: { type: Boolean, default: false },
})
const emit = defineEmits(['update:visible'])

const tokens = ref([])
const loading = ref(false)

// 新增表单
const form = reactive({ token: '', label: '', max_requests: 1500000 })
const adding = ref(false)

async function refresh() {
  loading.value = true
  try {
    tokens.value = await api.listTokens()
  } catch (e) {
    MessagePlugin.error('加载密钥列表失败:' + (e?.message || e))
  } finally {
    loading.value = false
  }
}

watch(() => props.visible, (v) => { if (v) refresh() })

function close() { emit('update:visible', false) }

async function addToken() {
  if (!form.token.trim()) { MessagePlugin.error('请输入密钥'); return }
  adding.value = true
  try {
    tokens.value = await api.addToken({
      token: form.token.trim(),
      label: form.label.trim(),
      max_requests: Number(form.max_requests) || 1500000,
    })
    form.token = ''; form.label = ''; form.max_requests = 1500000
    MessagePlugin.success('已添加密钥')
  } catch (e) {
    MessagePlugin.error('添加失败:' + (e?.message || e))
  } finally {
    adding.value = false
  }
}

async function removeToken(row) {
  const confirmDia = DialogPlugin.confirm({
    header: '删除密钥',
    body: `确认删除密钥「${row.label || row.token_masked}」?`,
    theme: 'warning',
    onConfirm: async () => {
      try {
        tokens.value = await api.deleteToken(row.id)
        MessagePlugin.success('已删除')
      } catch (e) {
        MessagePlugin.error('删除失败:' + (e?.message || e))
      }
      confirmDia.destroy()
    },
  })
}

async function toggleEnabled(row) {
  try {
    tokens.value = await api.updateToken(row.id, { enabled: !row.enabled })
  } catch (e) {
    MessagePlugin.error('更新失败:' + (e?.message || e))
    refresh()
  }
}

// 备注 / 上限就地编辑
const editingId = ref(null)
const editForm = reactive({ label: '', max_requests: 0 })
function startEdit(row) {
  editingId.value = row.id
  editForm.label = row.label
  editForm.max_requests = row.max_requests
}
async function saveEdit(row) {
  try {
    tokens.value = await api.updateToken(row.id, {
      label: editForm.label.trim(),
      max_requests: Number(editForm.max_requests) || 1500000,
    })
    editingId.value = null
    MessagePlugin.success('已保存')
  } catch (e) {
    MessagePlugin.error('保存失败:' + (e?.message || e))
  }
}

async function resetCount(row) {
  try {
    tokens.value = await api.resetToken(row.id)
    MessagePlugin.success('已重置当日计数')
  } catch (e) {
    MessagePlugin.error('重置失败:' + (e?.message || e))
  }
}

// 调整顺序:上移/下移后提交完整顺序
async function move(index, delta) {
  const arr = [...tokens.value]
  const j = index + delta
  if (j < 0 || j >= arr.length) return
  ;[arr[index], arr[j]] = [arr[j], arr[index]]
  try {
    tokens.value = await api.reorderTokens(arr.map((t) => t.id))
  } catch (e) {
    MessagePlugin.error('调整顺序失败:' + (e?.message || e))
    refresh()
  }
}

function usagePct(row) {
  if (!row.max_requests) return 0
  return Math.min(100, Math.round(row.request_count / row.max_requests * 100))
}
function usageTheme(row) {
  const p = usagePct(row)
  if (p >= 100) return 'danger'
  if (p >= 80) return 'warning'
  return 'success'
}

const summary = computed(() => {
  const total = tokens.value.length
  const enabled = tokens.value.filter((t) => t.enabled).length
  const used = tokens.value.reduce((s, t) => s + (t.request_count || 0), 0)
  const cap = tokens.value.reduce((s, t) => s + (t.max_requests || 0), 0)
  return { total, enabled, used, cap }
})
</script>

<template>
  <t-dialog
    :visible="visible" header="天地图密钥池管理"
    :width="760" :footer="false" @close="close" @update:visible="close"
  >
    <div class="tk-mgr">
      <p class="tk-tip">
        下载时按顺序轮询使用密钥,单个密钥当日请求达到上限后自动切换到下一个;
        全部用尽会提示并从第一个重新循环。计数只统计后端下载的瓦片请求,每日自动还原。
      </p>

      <!-- 新增密钥 -->
      <div class="tk-add">
        <t-input v-model="form.token" placeholder="粘贴天地图密钥(tk)" class="f-token" clearable />
        <t-input v-model="form.label" placeholder="备注(可选)" class="f-label" clearable />
        <t-input-number v-model="form.max_requests" :min="1" :step="100000"
          theme="normal" class="f-max" placeholder="每日上限" />
        <t-button theme="primary" :loading="adding" @click="addToken">添加</t-button>
      </div>

      <!-- 汇总 -->
      <div class="tk-summary">
        共 {{ summary.total }} 个密钥(启用 {{ summary.enabled }}) ·
        当日已用 {{ fmtNum(summary.used) }} / {{ fmtNum(summary.cap) }} 次
      </div>

      <!-- 列表 -->
      <div class="tk-list" v-loading="loading">
        <div v-if="!tokens.length && !loading" class="tk-empty">
          暂无密钥,请在上方添加。
        </div>
        <div v-for="(row, i) in tokens" :key="row.id" class="tk-row" :class="{ current: row.is_current }">
          <div class="tk-order">
            <t-button size="small" variant="text" shape="square"
              :disabled="i === 0" @click="move(i, -1)">↑</t-button>
            <t-button size="small" variant="text" shape="square"
              :disabled="i === tokens.length - 1" @click="move(i, 1)">↓</t-button>
          </div>
          <div class="tk-main">
            <div class="tk-line1">
              <t-tag v-if="row.is_current" theme="primary" size="small" variant="light">当前</t-tag>
              <template v-if="editingId === row.id">
                <t-input v-model="editForm.label" size="small" placeholder="备注" class="e-label" />
                <t-input-number v-model="editForm.max_requests" size="small" :min="1"
                  :step="100000" theme="normal" class="e-max" />
              </template>
              <template v-else>
                <span class="tk-label">{{ row.label || '(无备注)' }}</span>
                <code class="tk-code">{{ row.token_masked }}</code>
              </template>
            </div>
            <div class="tk-line2">
              <t-progress :percentage="usagePct(row)" :theme="usageTheme(row)"
                :label="false" class="tk-bar" />
              <span class="tk-count">{{ fmtNum(row.request_count) }} / {{ fmtNum(row.max_requests) }}</span>
            </div>
          </div>
          <div class="tk-actions">
            <t-switch :value="row.enabled" size="small" @change="toggleEnabled(row)" />
            <template v-if="editingId === row.id">
              <t-button size="small" theme="primary" @click="saveEdit(row)">保存</t-button>
              <t-button size="small" variant="text" @click="editingId = null">取消</t-button>
            </template>
            <template v-else>
              <t-button size="small" variant="text" @click="startEdit(row)">编辑</t-button>
              <t-button size="small" variant="text" @click="resetCount(row)">重置</t-button>
              <t-button size="small" variant="text" theme="danger" @click="removeToken(row)">删除</t-button>
            </template>
          </div>
        </div>
      </div>
    </div>
  </t-dialog>
</template>

<style scoped>
.tk-mgr { display: flex; flex-direction: column; gap: 10px; }
.tk-tip { font-size: 12px; color: #64748b; line-height: 1.6; margin: 0;
  background: #f1f5f9; padding: 8px 10px; border-radius: 6px; }
.tk-add { display: flex; gap: 8px; align-items: center; }
.tk-add .f-token { flex: 1 1 auto; }
.tk-add .f-label { flex: 0 0 130px; }
.tk-add .f-max { flex: 0 0 150px; }
.tk-summary { font-size: 12px; color: #0369a1; font-weight: 600; }
.tk-list { max-height: 48vh; overflow-y: auto; display: flex;
  flex-direction: column; gap: 6px; min-height: 60px; }
.tk-empty { text-align: center; color: #94a3b8; padding: 24px 0; font-size: 13px; }
.tk-row { display: flex; align-items: center; gap: 8px; padding: 8px 10px;
  border: 1px solid #e2e8f0; border-radius: 8px; background: #fff; }
.tk-row.current { border-color: #0ea5e9; background: #f0f9ff; }
.tk-order { display: flex; flex-direction: column; }
.tk-main { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.tk-line1 { display: flex; align-items: center; gap: 8px; }
.tk-label { font-weight: 600; font-size: 13px; color: #334155;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 160px; }
.tk-code { font-size: 12px; color: #94a3b8; font-family: monospace; }
.tk-line2 { display: flex; align-items: center; gap: 10px; }
.tk-bar { flex: 1 1 auto; }
.tk-count { font-size: 11px; color: #64748b; white-space: nowrap; }
.tk-actions { display: flex; align-items: center; gap: 6px; flex: 0 0 auto; }
.e-label { width: 120px; }
.e-max { width: 130px; }
</style>

