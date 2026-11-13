<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { useAuth } from '../stores/auth'

const auth = useAuth()
const members = ref<any[]>([]), roles = ref<any[]>([]), permissions = ref<any[]>([])
const error = ref(''), notice = ref(''), busy = ref(false), editing = ref<any>(null)
const member = ref({ email: '', display_name: '', password: '' })
const selectedRoles = ref<string[]>([]), roleId = ref(''), roleCode = ref(''), roleName = ref(''), rolePermissions = ref<string[]>([])
const labels: Record<string, string> = { 'knowledge.create': '创建知识库', 'agent.execute': '执行 Agent', 'evaluation.run': '运行评测', 'monitoring.read': '查看监控' }
async function load() {
  const result = await Promise.all([api('/members'), api('/roles'), api('/permissions')])
  members.value = result[0]; roles.value = result[1]; permissions.value = result[2]
}
async function perform(fn: () => Promise<void>) {
  busy.value = true; error.value = ''; notice.value = ''
  try { await fn(); await load() } catch (e: any) { error.value = e.message } finally { busy.value = false }
}
onMounted(() => { if (auth.user?.is_tenant_admin) perform(async () => {}) })
async function createMember() {
  await perform(async () => {
    await api('/members', { method: 'POST', body: JSON.stringify(member.value) })
    member.value = { email: '', display_name: '', password: '' }; notice.value = '成员已创建，可在列表中分配角色。'
  })
}
function editMember(value: any) { editing.value = value; selectedRoles.value = [...value.role_ids] }
async function saveRoles() {
  await perform(async () => { await api(`/members/${editing.value.id}/roles`, { method: 'PUT', body: JSON.stringify({ role_ids: selectedRoles.value }) }); editing.value = null; notice.value = '成员角色已更新。' })
}
async function toggle(value: any) {
  await perform(async () => { await api(`/members/${value.id}`, { method: 'PATCH', body: JSON.stringify({ is_active: !value.is_active }) }); notice.value = value.is_active ? '成员已停用，现有访问凭据将被拒绝。' : '成员已启用。' })
}
function editRole(id: string) {
  roleId.value = id
  const value = roles.value.find(r => r.id === id)
  roleCode.value = value?.code || ''; roleName.value = value?.name || ''; rolePermissions.value = [...(value?.permission_codes || [])]
}
async function saveRole() {
  await perform(async () => {
    const values: any = { name: roleName.value, permission_codes: rolePermissions.value }
    if (!roleId.value) values.code = roleCode.value
    await api(roleId.value ? `/roles/${roleId.value}` : '/roles', { method: roleId.value ? 'PATCH' : 'POST', body: JSON.stringify(values) })
    editRole(''); notice.value = '角色已保存，相关权限变更立即生效。'
  })
}
</script>

<template>
  <p v-if="!auth.user?.is_tenant_admin" class="notice">仅租户管理员可以管理成员和角色。</p>
  <div v-else class="member-administration">
    <div v-if="error" class="error" role="alert">{{ error }}</div><div v-if="notice" class="notice" role="status">{{ notice }}</div>
    <div class="panel padded"><div class="section-heading"><h2>团队成员</h2><span class="muted">{{ members.length }} 个账号</span></div>
      <div class="table-scroll"><table><thead><tr><th>成员</th><th>角色</th><th>状态</th><th>管理</th></tr></thead><tbody>
        <tr v-for="value in members" :key="value.id"><td><strong>{{ value.display_name || '未设置姓名' }}</strong><small class="member-email">{{ value.email }}</small></td>
          <td>{{ value.is_tenant_admin ? '租户管理员' : roles.filter(r => value.role_ids.includes(r.id)).map(r => r.name).join('、') || '未分配角色' }}</td>
          <td><span class="badge" :class="value.is_active ? 'success' : ''">{{ value.is_active ? '已启用' : '已停用' }}</span></td>
          <td class="table-actions"><button :disabled="busy" :aria-label="'分配角色 ' + value.email" @click="editMember(value)">分配角色</button><button :disabled="busy || value.id === auth.user.id" :aria-label="(value.is_active ? '停用 ' : '启用 ') + value.email" @click="toggle(value)">{{ value.is_active ? '停用' : '启用' }}</button></td>
        </tr>
      </tbody></table></div>
      <form v-if="editing" class="role-assignment" @submit.prevent="saveRoles"><h3>为 {{ editing.display_name || editing.email }} 分配角色</h3><div class="choice-list"><label v-for="role in roles" :key="role.id"><input v-model="selectedRoles" type="checkbox" :value="role.id"/>{{ role.name }}</label></div><div class="admin-actions"><button class="primary" :disabled="busy">保存成员角色</button><button type="button" class="secondary" @click="editing = null">取消</button></div></form>
    </div>
    <div class="two-columns admin-forms">
      <form class="panel padded" @submit.prevent="createMember"><h2>创建成员</h2><p class="muted">创建普通账号，按职责分配角色与资料权限。</p><label>成员姓名<input v-model="member.display_name" required maxlength="100"/></label><label>成员邮箱<input v-model="member.email" type="email" required autocomplete="off"/></label><label>初始密码<input v-model="member.password" type="password" minlength="8" required autocomplete="new-password"/></label><button class="primary" :disabled="busy">创建成员</button></form>
      <form class="panel padded" @submit.prevent="saveRole"><h2>角色与操作权限</h2><label>选择角色<select :value="roleId" aria-label="选择管理角色" @change="editRole(($event.target as HTMLSelectElement).value)"><option value="">创建新角色</option><option v-for="role in roles" :key="role.id" :value="role.id">{{ role.name }}</option></select></label><label>角色代码<input v-model="roleCode" required :disabled="!!roleId" pattern="[a-zA-Z0-9_-]+" maxlength="80"/></label><label>角色名称<input v-model="roleName" required maxlength="100"/></label><div class="choice-list"><label v-for="permission in permissions" :key="permission.code"><input v-model="rolePermissions" type="checkbox" :value="permission.code"/>{{ labels[permission.code] || permission.description || permission.code }}</label></div><p class="format-note">操作权限决定可执行的功能，具体文档仍需满足知识库与文档授权。</p><button class="primary" :disabled="busy">保存角色</button></form>
    </div>
  </div>
</template>

<style scoped>
.member-email{display:block;margin-top:6px;color:#84918a}.admin-forms{margin-top:24px}.admin-forms h2{font-size:19px;margin:0 0 16px}.choice-list{display:flex;flex-wrap:wrap;gap:14px;margin:18px 0}.choice-list label{display:flex;flex-direction:row;align-items:center;gap:8px;margin:0}.choice-list input{width:auto}.role-assignment{border-top:1px solid #e4e9df;margin-top:20px;padding-top:15px}.admin-actions{display:flex;gap:12px}.member-administration .table-actions{white-space:nowrap}@media(max-width:700px){.admin-actions{flex-wrap:wrap}.admin-forms{grid-template-columns:1fr}}
</style>
