<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'

const props = defineProps<{ target: { id: string, title: string, kind: 'knowledge' | 'document' } }>()
const emit = defineEmits<{ (event: 'close'): void, (event: 'saved'): void }>()
const error = ref(''), busy = ref(false), ready = ref(false), mode = ref(''), entries = ref<any[]>([])
const members = ref<any[]>([]), roles = ref<any[]>([]), subject = ref(''), level = ref('read')
const endpoint = props.target.kind === 'knowledge' ? `/knowledge-bases/${props.target.id}/access` : `/documents/${props.target.id}/access`
onMounted(async () => {
  busy.value = true
  try {
    const [value, subjects] = await Promise.all([api(endpoint), api(`/authorization/subjects?${props.target.kind === 'knowledge' ? 'knowledge_base_id' : 'document_id'}=${props.target.id}`)])
    mode.value = value.visibility || value.acl_mode; entries.value = value.entries
    members.value = subjects.members; roles.value = subjects.roles; ready.value = true
  } catch (e: any) { error.value = e.message } finally { busy.value = false }
})
function name(entry: any) {
  return entry.user_id ? (members.value.find(m => m.id === entry.user_id)?.display_name || members.value.find(m => m.id === entry.user_id)?.email || '成员') : roles.value.find(r => r.id === entry.role_id)?.name || '角色'
}
function add() {
  if (!subject.value) return
  const [kind, id] = subject.value.split(':')
  if (entries.value.some(e => e[kind] === id)) { error.value = '该主体已有授权，请先移除旧授权。'; return }
  entries.value.push({ [kind]: id, access_level: level.value }); subject.value = ''; error.value = ''
}
async function save() {
  busy.value = true; error.value = ''
  try {
    await api(endpoint, { method: 'POST', body: JSON.stringify({ [props.target.kind === 'knowledge' ? 'visibility' : 'acl_mode']: mode.value, entries: entries.value }) })
    emit('saved'); emit('close')
  } catch (e: any) { error.value = e.message } finally { busy.value = false }
}
</script>

<template>
  <div class="modal-backdrop" @click.self="!busy && emit('close')"><section class="access-dialog panel padded" role="dialog" aria-modal="true" aria-label="资料权限管理">
    <div class="section-heading"><h2>资料权限</h2><button class="secondary" :disabled="busy" aria-label="关闭权限管理" @click="emit('close')">关闭</button></div><p class="muted">{{ target.title }}</p>
    <div v-if="error" class="error" role="alert">{{ error }}</div>
    <template v-if="ready">
      <label>{{ target.kind === 'knowledge' ? '知识库可见范围' : '文档权限模式' }}<select v-model="mode" aria-label="资料可见范围"><template v-if="target.kind === 'knowledge'"><option value="private">私有，仅所有者、管理员及授权主体</option><option value="tenant">本租户成员可读</option></template><template v-else><option value="inherit">继承知识库权限</option><option value="restricted">进一步限制，只允许文档授权主体</option></template></select></label>
      <p class="format-note">文档授权只能收紧知识库范围。更改权限会立即影响检索、历史答案和引用；管理员始终保留管理能力。</p>
      <div class="access-add"><label>授权主体<select v-model="subject" aria-label="授权主体"><option value="">选择成员或角色</option><optgroup label="成员"><option v-for="value in members" :key="value.id" :value="'user_id:' + value.id">{{ value.display_name || value.email }}{{ value.is_active ? '' : '（已停用）' }}</option></optgroup><optgroup label="角色"><option v-for="value in roles" :key="value.id" :value="'role_id:' + value.id">{{ value.name }}</option></optgroup></select></label><label>访问级别<select v-model="level" aria-label="授权级别"><option value="read">读取</option><option value="edit">编辑</option><option value="manage">管理</option></select></label><button class="secondary" :disabled="!subject" @click="add">添加授权</button></div>
      <div class="access-entries"><div v-for="(entry, i) in entries" :key="entry.user_id || entry.role_id" class="access-entry"><strong>{{ name(entry) }}</strong><span class="badge">{{ { read: '读取', edit: '编辑', manage: '管理' }[entry.access_level as 'read' | 'edit' | 'manage'] }}</span><button class="text-danger" :aria-label="'移除授权 ' + name(entry)" @click="entries.splice(i, 1)">移除</button></div><p v-if="!entries.length" class="muted">暂无额外授权</p></div>
      <button class="primary" :disabled="busy" @click="save">保存资料权限</button>
    </template>
  </section></div>
</template>

<style scoped>
.access-dialog{width:min(760px,calc(100vw - 32px));max-height:90vh;overflow:auto}.access-dialog h2{margin:0;font-size:21px}.access-add{display:flex;gap:12px;align-items:end;margin-top:20px}.access-add label:first-child{flex:1}.access-add button{margin-bottom:18px}.access-entry{display:flex;gap:20px;align-items:center;padding:12px 0;border-bottom:1px solid #e6ebdf}.access-entry strong{flex:1;font-size:13px}.access-entries{margin-bottom:24px}@media(max-width:700px){.access-add{flex-wrap:wrap}.access-add label:first-child{flex-basis:100%}.access-entry{gap:12px}.access-entry strong{overflow-wrap:anywhere}}
</style>
