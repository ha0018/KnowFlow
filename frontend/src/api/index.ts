// Keep bearer credentials in session storage; expire them when the tab session ends.
export const tokens = {
  get access() { return sessionStorage.getItem('kf_access') || '' },
  get refresh() { return sessionStorage.getItem('kf_refresh') || '' },
  set(access: string, refresh: string) { sessionStorage.setItem('kf_access', access); sessionStorage.setItem('kf_refresh', refresh) },
  clear() { sessionStorage.removeItem('kf_access'); sessionStorage.removeItem('kf_refresh') },
}
let refreshing: Promise<void> | null = null
export async function authorizedFetch(path: string, options: RequestInit = {}, retry = true): Promise<Response> {
  const headers = new Headers(options.headers)
  if (tokens.access) headers.set('Authorization', `Bearer ${tokens.access}`)
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  const response = await fetch(`/api/v1${path}`, { ...options, headers })
  if (response.status === 401 && retry && tokens.refresh && path !== '/auth/refresh' && path !== '/auth/login') {
    refreshing ||= (async () => {
      const r = await fetch('/api/v1/auth/refresh', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ refresh: tokens.refresh }) })
      if (!r.ok) { tokens.clear(); throw new Error('登录已过期，请重新登录') }
      const data = await r.json(); tokens.set(data.access, data.refresh)
    })().finally(() => { refreshing = null })
    await refreshing
    return authorizedFetch(path, options, false)
  }
  return response
}
export async function api(path: string, options: RequestInit = {}) {
  const r = await authorizedFetch(path, options)
  if (r.status === 204) return null
  const body = await r.json()
  if (!r.ok) throw new Error(typeof body.error === 'string' ? body.error : JSON.stringify(body.error || body))
  return body
}
export const list = (value: any): any[] => Array.isArray(value) ? value : value.results || []
