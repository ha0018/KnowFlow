<script setup lang="ts">
import { onUnmounted, ref } from 'vue'
import { api, authorizedFetch } from '../api'
import { readSSE } from '../api/sse'

const props = defineProps<{ kbId: string, documents: any[] }>()
const emit = defineEmits<{ (event: 'open-source', source: any): void }>()
const question = ref(''), running = ref(false), runId = ref(''), answer = ref(''), error = ref(''), status = ref('')
const steps = ref<any[]>([]), references = ref<any[]>([]), provider = ref(''), selectedDocument = ref('')
let controller: AbortController | null = null
const names: Record<string, string> = { KnowledgeSearchTool: '知识检索', DocumentSummaryTool: '文档摘要', DataAnalysisTool: '业务统计' }
const statuses: Record<string, string> = { running: '执行中', completed: '已完成', failed: '失败', interrupted: '已中断', cancelled: '已取消', timed_out: '已超时', rejected: '已拒绝' }

async function run() {
  if (running.value || !question.value.trim()) return
  running.value = true; error.value = ''; answer.value = ''; status.value = 'running'; steps.value = []; references.value = []; runId.value = ''
  controller = new AbortController()
  try {
    const conversation = await api('/conversations', { method: 'POST', body: JSON.stringify({ kb_ids: props.kbId ? [props.kbId] : [] }) })
    const response = await authorizedFetch(`/conversations/${conversation.id}/agent:stream`, { method: 'POST', signal: controller.signal, headers: { 'Idempotency-Key': crypto.randomUUID() }, body: JSON.stringify({ question: question.value }) })
    await readSSE(response, ({ event, data }) => {
      if (event === 'meta') { runId.value = data.run_id; provider.value = data.provider }
      if (event === 'tool') {
        const previous = steps.value.findIndex(s => s.step === data.step)
        if (previous >= 0) steps.value[previous] = data
        else steps.value.push(data)
      }
      if (event === 'references') references.value = data.sources
      if (event === 'delta') answer.value += data.text
      if (event === 'error') error.value = `任务停止：${data.code} · ${data.trace_id}`
      if (event === 'done') status.value = data.status
    })
  } catch (e: any) {
    if (e.name !== 'AbortError') error.value = e.message || '任务执行失败'
  } finally {
    if (runId.value) {
      try {
        const result = await api(`/runs/${runId.value}`)
        answer.value = result.content; references.value = result.references; status.value = result.status
        steps.value = (result.tool_calls || []).sort((a: any, b: any) => a.step_index - b.step_index).map((s: any) => ({ step: s.step_index, name: s.tool_name, status: s.status, duration_ms: s.latency_ms, error_code: s.error_code }))
        if (result.error_code) error.value = `停止原因：${result.error_code}`
      } catch (e: any) { error.value = e.message }
    }
    running.value = false
  }
}
async function stop() {
  if (runId.value) await api(`/runs/${runId.value}/cancel`, { method: 'POST' })
  controller?.abort()
}
function summary() {
  if (selectedDocument.value) question.value = `请总结文档 ${selectedDocument.value}，保留来源并指出内容是否截断。`
}
onUnmounted(() => controller?.abort())
</script>

<template>
  <section class="panel padded controlled-agent">
    <span class="eyebrow">CONTROLLED AGENT</span><div class="section-heading"><h2>让工具协作完成任务</h2><span v-if="status" class="badge" :class="status === 'completed' ? 'success' : ''">{{ statuses[status] || status }}</span></div>
    <p class="muted">最多 4 次只读工具调用 · 单工具 10 秒 · 任务总计 60 秒</p>
    <p class="format-note">{{ provider && provider !== 'demo' ? '使用已配置模型提出调用；权限、参数和预算由服务端控制。' : '演示模式使用规则规划和原文摘录。配置真实模型后可验证模型自主提出调用。' }}</p>
    <div class="agent-examples"><button class="secondary" :disabled="running" @click="question = '退款超过30天还能申请吗？'">检索退款规定</button><button class="secondary" :disabled="running" @click="question = '统计2026-01-01至2026-12-31按类别分组的问题'">统计演示问题</button><select v-model="selectedDocument" aria-label="Agent 摘要文档" :disabled="running" @change="summary"><option value="">选择文档进行摘要</option><option v-for="doc in documents.filter(d => d.active_version)" :key="doc.id" :value="doc.id">{{ doc.title }}</option></select></div>
    <form @submit.prevent="run"><label>Agent 任务<textarea v-model="question" aria-label="Agent 任务" rows="3" maxlength="4000" required :disabled="running" placeholder="描述要完成的任务，统计时请给出明确起止日期。"/></label><div class="agent-run-actions"><small class="muted">使用页面当前选择的知识库范围；统计工具只处理当前租户演示数据。</small><button v-if="running" type="button" class="primary" @click="stop">停止任务</button><button v-else class="primary" :disabled="!question.trim()">开始任务</button></div></form>
    <p v-if="error" class="info-note text-danger" role="alert">{{ error }}</p>
    <div v-if="steps.length" class="agent-steps"><h3>工具执行记录</h3><div v-for="step in steps" :key="step.step" class="agent-step"><span class="result-rank">{{ step.step }}</span><strong>{{ names[step.name] || step.name }}</strong><span class="badge" :class="step.status === 'completed' ? 'success' : ''">{{ statuses[step.status] || step.status }}</span><small>{{ step.duration_ms != null ? step.duration_ms + ' ms' : '' }} {{ step.error_code || '' }}</small></div></div>
    <div v-if="answer" class="agent-answer"><h3>任务结果</h3><p class="pre-wrap">{{ answer }}</p><div class="references"><button v-for="ref in references" :key="ref.id" @click="emit('open-source', ref)">{{ ref.source_id }} · {{ ref.title }} · {{ ref.kind === 'analysis' ? '统计快照' : 'V' + ref.version }}</button></div></div>
  </section>
</template>

<style scoped>
.controlled-agent{margin-bottom:24px}.controlled-agent .section-heading{margin:12px 0}.controlled-agent h2{font-size:23px;font-weight:500}.controlled-agent h3{font-size:15px;font-weight:500;margin:24px 0 14px}.agent-examples{display:flex;gap:12px;flex-wrap:wrap;margin:24px 0 16px}.agent-examples select{width:auto;max-width:100%}.agent-run-actions{display:flex;align-items:center;justify-content:space-between;gap:16px}.agent-steps{border-top:1px solid #e9ede5;margin-top:24px}.agent-step{display:flex;gap:16px;align-items:center;padding:12px 0;border-bottom:1px solid #eef1ea;font-size:12px}.agent-step .result-rank{position:static;display:inline-block;min-width:20px;font-size:13px}.agent-step strong{font-weight:500;min-width:90px}.agent-step small{color:#8b978e}.agent-answer{font-size:13px;line-height:1.9}.agent-answer>p{background:#f6f8f2;padding:18px;border-radius:8px}@media(max-width:700px){.agent-run-actions{flex-wrap:wrap}.agent-run-actions>button{width:100%;justify-content:center}.agent-step{flex-wrap:wrap}.agent-step small{width:100%;padding-left:36px}.agent-examples select{width:100%}}
</style>
