import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api, tokens } from '../api'

export const useAuth = defineStore('auth', () => {
  const user = ref<any>(null)
  const ready = ref(false)
  async function login(email: string, password: string) {
    const data = await api('/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) })
    tokens.set(data.access, data.refresh)
    user.value = data.user
  }
  async function restore() {
    try { if (tokens.access) user.value = await api('/auth/me') } catch { tokens.clear() }
    ready.value = true
  }
  async function logout() {
    try { await api('/auth/logout', { method: 'POST', body: JSON.stringify({ refresh: tokens.refresh }) }) }
    finally { tokens.clear(); user.value = null }
  }
  return { user, ready, login, restore, logout }
})
