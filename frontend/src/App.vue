<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { BookOpen, LayoutDashboard, Library, MessageSquare, Search, Workflow, ChartNoAxesCombined, Activity, ArrowUpRight, LogOut, ArrowRight, Layers } from 'lucide-vue-next'
import { useAuth } from './stores/auth'
const auth = useAuth()
const email = ref('admin@acme.demo'), password = ref(''), error = ref(''), busy = ref(false)
const nav = computed(() => [
  { path: 'overview', name: '工作台', icon: LayoutDashboard }, { path: 'knowledge', name: '知识空间', icon: Library },
  { path: 'chat', name: '知识问答', icon: MessageSquare }, { path: 'retrieval', name: '检索调试', icon: Search },
  { path: 'tools', name: '只读工具', icon: Workflow }, { path: 'evaluation', name: '效果评估', icon: ChartNoAxesCombined },
  { path: 'monitoring', name: '调用记录', icon: Activity },
  ...(auth.user?.is_tenant_admin ? [{ path: 'members', name: '成员与角色', icon: Layers }] : []),
])
onMounted(() => auth.restore())
async function login() { busy.value = true; error.value = ''; try { await auth.login(email.value, password.value) } catch (e: any) { error.value = e.message } finally { busy.value = false } }
</script>

<template>
  <div v-if="!auth.ready" class="loading">正在连接知识空间…</div>
  <div v-else-if="!auth.user" class="login-page">
    <section class="login-story"><div class="brand light"><BookOpen :size="27"/> KnowFlow<span>AI</span></div><div class="story-main"><span class="eyebrow">A PLACE FOR WHAT YOUR TEAM KNOWS</span><h1>让知识流动，<br/>让答案有据可循。</h1><p>连接团队的经验与资料。<br/>从每一份文档，到每一次有依据的回答。</p><div class="story-note"><Layers :size="20"/><span>团队知识 · 清晰权限 · 可追溯的来源</span></div></div><small>KNOWFLOW / ENTERPRISE KNOWLEDGE WORKSPACE</small></section>
    <section class="login-form"><form @submit.prevent="login"><span class="eyebrow">WELCOME BACK</span><h2>进入你的知识空间</h2><p class="muted">使用管理员为你创建的企业账号登录</p><label>邮箱地址<input v-model="email" type="email" required autocomplete="username" placeholder="you@company.com"/></label><label>登录密码<input v-model="password" type="password" required autocomplete="current-password" placeholder="输入初始化时设置的密码"/></label><div v-if="error" role="alert" class="error">{{ error }}</div><button class="primary full" :disabled="busy">{{ busy ? '正在登录…' : '登录工作空间' }}<ArrowRight :size="17"/></button><p class="login-help">首次使用？请先按照 README 初始化演示账号。</p></form></section>
  </div>
  <div v-else class="app-shell">
    <aside class="sidebar"><div class="brand"><BookOpen :size="26"/> KnowFlow<span>AI</span></div><div class="workspace-label"><span class="workspace-avatar">{{ auth.user.tenant_name[0] }}</span><div>{{ auth.user.tenant_name }}<small>团队工作空间</small></div><span class="workspace-dot"></span></div><div class="nav-label">工作空间</div><nav><RouterLink v-for="item in nav" :key="item.path" :to="'/' + item.path" :aria-label="item.name"><component :is="item.icon" :size="18"/><span>{{ item.name }}</span><ArrowUpRight v-if="item.path === 'chat'" class="nav-arrow" :size="14"/></RouterLink></nav><div class="sidebar-bottom"><div class="sidebar-tip"><span class="tiny-label">BUILT ON TRUST</span><p>每一个答案，<br/>都有迹可循。</p><BookOpen :size="27"/></div><div class="user"><span class="avatar">{{ (auth.user.display_name || '用')[0] }}</span><div>{{ auth.user.display_name }}<small>{{ auth.user.is_tenant_admin ? '租户管理员' : '团队成员' }}</small></div><button class="icon-button" aria-label="退出登录" @click="auth.logout"><LogOut :size="16"/></button></div></div></aside>
    <main class="main"><header class="topbar"><span>工作空间 <span class="slash">/</span> {{ nav.find(n => $route.path === '/' + n.path)?.name }}</span><div><span class="status-dot"></span> 私有团队空间 <span class="top-divider">|</span><span class="avatar small">{{ (auth.user.display_name || '用')[0] }}</span></div></header><RouterView :key="auth.user.id"/></main>
  </div>
</template>
