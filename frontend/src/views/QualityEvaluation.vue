<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from '../api'

const datasets = ref<any[]>([]), runs = ref<any[]>([]), datasetId = ref(''), baseline = ref('B1'), split = ref('tuning')
const answers = ref(false), rewrite = ref(false), busy = ref(false), error = ref(''), detail = ref<any>(null), resultId = ref('')
const selectedDataset = computed(() => datasets.value.find(d => d.id === datasetId.value))
const selectedResult = computed(() => detail.value?.results.find((r: any) => r.id === resultId.value))
const review = ref<any>({})
const score = (value: any) => typeof value === 'number' ? value.toFixed(3) : '—'
const ms = (value: any) => typeof value === 'number' ? `${value.toFixed(1)} ms` : '—'
const message = (e: any) => { error.value = e.message || '评估操作失败' }

async function refreshRuns() { runs.value = await api('/evaluations/runs') }
async function load() {
  try {
    datasets.value = await api('/evaluations/datasets')
    datasetId.value ||= datasets.value[0]?.id || ''
    await refreshRuns()
  } catch (e) { message(e) }
}
watch(selectedDataset, dataset => { split.value = dataset?.split === 'stratified' ? 'tuning' : 'development' })
watch(selectedResult, row => {
  review.value = { answer_pass: null, refusal_correct: null, facts_total: 0, facts_supported: 0, cited_claims_total: 0, cited_claims_supported: 0, notes: '', ...(row?.human_review || {}) }
})
async function evaluate() {
  busy.value = true; error.value = ''
  try {
    await api('/evaluations/runs', { method: 'POST', body: JSON.stringify({ dataset_id: datasetId.value, baseline: baseline.value, split: split.value, answers: answers.value, rewrite: rewrite.value }) })
    await refreshRuns()
  } catch (e) { message(e) } finally { busy.value = false }
}
async function openRun(id: string) {
  busy.value = true; error.value = ''
  try { detail.value = await api(`/evaluations/runs/${id}`); resultId.value = detail.value.results[0]?.id || '' }
  catch (e) { message(e) } finally { busy.value = false }
}
async function saveReview() {
  busy.value = true; error.value = ''
  try {
    const { reviewer_id, reviewed_at, ...data } = review.value
    const response = await api(`/evaluations/runs/${detail.value.run_id}/results/${resultId.value}/review`, { method: 'PUT', body: JSON.stringify(data) })
    selectedResult.value.human_review = response.human_review; detail.value.metrics = response.metrics
    await refreshRuns()
  } catch (e) { message(e) } finally { busy.value = false }
}
async function exportRun(id: string) {
  try {
    const report = await api(`/evaluations/runs/${id}`)
    const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }))
    const link = document.createElement('a'); link.href = url; link.download = `knowflow-eval-${id.slice(0, 8)}.json`; link.click(); URL.revokeObjectURL(url)
  } catch (e) { message(e) }
}
onMounted(load)
const polling = setInterval(() => { if (runs.value.some(r => ['PENDING', 'RUNNING'].includes(r.status))) refreshRuns().catch(message) }, 2500)
onUnmounted(() => clearInterval(polling))
</script>

<template>
  <div v-if="error" class="info-note text-danger" role="alert">{{ error }}</div>
  <section class="panel padded quality-controls">
    <span class="eyebrow">RETRIEVAL EXPERIMENT</span>
    <h2>在同一份证据上比较</h2>
    <p class="muted">B0 向量基线 · B1 混合检索 · B2 混合检索与重排</p>
    <div class="quality-selects">
      <label>评估数据集<select v-model="datasetId" aria-label="评估数据集"><option v-for="d in datasets" :key="d.id" :value="d.id">{{ d.name }} · {{ d.version }}</option></select></label>
      <label>数据分集<select v-model="split" aria-label="数据分集"><option v-for="(count, name) in selectedDataset?.splits" :key="name" :value="name">{{ name === 'tuning' ? '调参集' : name === 'holdout' ? '保留集 · 最终确认' : '开发集' }} · {{ count }} 题</option></select></label>
      <label>实验配置<select v-model="baseline" aria-label="实验配置"><option value="B0">B0 · 仅向量</option><option value="B1">B1 · 向量 + 关键词 / RRF</option><option value="B2">B2 · 混合检索 + 重排</option></select></label>
    </div>
    <div class="quality-options"><label><input v-model="rewrite" type="checkbox"/>启用查询改写</label><label><input v-model="answers" type="checkbox"/>生成回答供人工复核</label><button class="primary" :disabled="busy || !datasetId" @click="evaluate">运行评估</button></div>
    <p v-if="split === 'holdout'" class="info-note">保留集用于冻结配置后的最终确认。若据此调整参数，需要建立新的保留集。</p>
    <p class="format-note">演示模型用于验证流程；词项重排不代表神经模型效果。引用有效性自动检查，事实正确性与引用支撑由人工复核。</p>
  </section>
  <div class="section-heading"><h2>实验记录</h2><span class="muted">失败样本计入统计分母</span></div>
  <div class="panel table-scroll"><table class="quality-runs"><thead><tr><th>配置 / 分集</th><th>DocRecall@5</th><th>EvidenceHit@5</th><th>MRR@5</th><th>检索 P95</th><th>状态</th><th>结果</th></tr></thead><tbody>
    <tr v-for="run in runs" :key="run.id" class="eval-row"><td><strong>{{ run.config_snapshot.baseline || run.config_snapshot.mode }}</strong><small>{{ run.config_snapshot.split || 'development' }} · {{ run.id.slice(0, 8) }}</small></td><td>{{ score(run.metrics.doc_recall_at_5) }}</td><td>{{ score(run.metrics.evidence_hit_at_5) }}</td><td>{{ score(run.metrics.mrr_at_5) }}</td><td>{{ ms(run.metrics.retrieval_p95_ms) }}</td><td><span class="badge" :class="run.status === 'COMPLETED' ? 'success' : ''">{{ run.status }}</span></td><td><div class="table-actions"><button :disabled="busy || ['PENDING', 'RUNNING'].includes(run.status)" @click="openRun(run.id)">查看逐题</button><button :disabled="busy" @click="exportRun(run.id)">导出</button></div></td></tr>
    <tr v-if="!runs.length"><td colspan="7" class="muted">运行实验后，这里会保留配置、耗时和结果。</td></tr>
  </tbody></table></div>
  <section v-if="detail" class="panel padded quality-detail">
    <div class="section-heading"><h2>逐题记录</h2><small>{{ detail.metrics.human_review?.reviewed || 0 }} / {{ detail.metrics.case_count || 0 }} 题已复核</small></div>
    <label>选择题目<select v-model="resultId" aria-label="选择题目"><option v-for="row in detail.results" :key="row.id" :value="row.id">{{ row.case_key || row.case_id.slice(0, 8) }} · {{ row.question }}</option></select></label>
    <article v-if="selectedResult" class="quality-case">
      <div class="inline"><span class="badge">{{ selectedResult.category }}</span><span v-if="selectedResult.error" class="text-danger">{{ selectedResult.error }}</span><small>耗时 {{ ms(selectedResult.trace.stages_ms?.total) }}</small></div>
      <h3>{{ selectedResult.question }}</h3>
      <div v-if="selectedResult.redacted" class="info-note">此题包含当前已不可读的证据，回答、来源及复核内容已隐藏。</div>
      <template v-else>
        <div class="quality-evidence"><strong>标准要点</strong><p>{{ selectedResult.expected_answer }}</p></div>
        <div class="quality-evidence"><strong>实际回答</strong><p class="pre-wrap">{{ selectedResult.answer || '此实验未生成回答；可勾选“生成回答供人工复核”后运行新实验。' }}</p></div>
        <p class="format-note">证据命中 {{ score(selectedResult.metrics.evidence_hit_at_5) }} · MRR {{ score(selectedResult.metrics.mrr_at_5) }} · Precision@5 {{ score(selectedResult.metrics.precision_at_5) }} · 引用有效率 {{ score(selectedResult.metrics.citation_validity) }}</p>
        <details v-for="source in selectedResult.sources" :key="source.chunk_id" class="quality-source"><summary>{{ source.source_id }} · {{ source.title }} · V{{ source.version }}</summary><p class="pre-wrap">{{ source.content }}</p><small>{{ JSON.stringify(source.locator) }}</small></details>
        <details><summary>配置与分段耗时</summary><pre class="result-json">{{ JSON.stringify({ config: detail.config, trace: selectedResult.trace }, null, 2) }}</pre></details>
        <form v-if="detail.config.answers && !selectedResult.error" class="quality-review" @submit.prevent="saveReview">
          <h3>人工复核</h3><p class="format-note">逐项核对原文与实际回答。未复核的指标显示为空，不自动判为通过。</p>
          <div class="quality-selects"><label>答案是否通过<select v-model="review.answer_pass"><option :value="null">不适用 / 待复核</option><option :value="true">通过</option><option :value="false">未通过</option></select></label><label>拒答或冲突处理是否正确<select v-model="review.refusal_correct"><option :value="null">不适用 / 待复核</option><option :value="true">正确</option><option :value="false">不正确</option></select></label></div>
          <div class="quality-selects"><label>可核查事实数<input v-model.number="review.facts_total" type="number" min="0" max="1000" required/></label><label>有证据支撑的事实数<input v-model.number="review.facts_supported" type="number" min="0" :max="review.facts_total" required/></label><label>带引用断言数<input v-model.number="review.cited_claims_total" type="number" min="0" max="1000" required/></label><label>引用支撑的断言数<input v-model.number="review.cited_claims_supported" type="number" min="0" :max="review.cited_claims_total" required/></label></div>
          <label>复核依据<textarea v-model="review.notes" aria-label="复核依据" rows="3" maxlength="4000" required placeholder="记录支撑或不支撑的具体原文、错误与缺失要点。"/></label><button class="primary" :disabled="busy">保存复核</button><small v-if="selectedResult.human_review.reviewed_at" class="muted">已保存 {{ new Date(selectedResult.human_review.reviewed_at).toLocaleString('zh-CN') }}</small>
        </form>
      </template>
    </article>
  </section>
</template>

<style scoped>
.quality-controls>h2{font-size:23px;font-weight:500;margin:12px 0}.quality-selects{display:flex;gap:16px;flex-wrap:wrap}.quality-selects>label{flex:1 1 180px;min-width:0}.quality-options{display:flex;align-items:center;gap:22px;flex-wrap:wrap;margin:8px 0 18px}.quality-options>label{display:flex;align-items:center;gap:8px;font-size:12px;margin:0;flex-direction:row}.quality-options input{width:auto;margin:0}.quality-options .primary{margin-left:auto}.quality-runs{min-width:850px}.quality-runs .eval-row{display:table-row;margin:0;padding:0;background:none;border:0}.quality-runs small{display:block;font-size:10px;color:#8b978e;margin-top:7px}.quality-detail{margin-top:24px;min-width:0}.quality-detail .section-heading{margin-top:0}.quality-case h3{font-weight:500;font-size:17px;line-height:1.7;margin:22px 0 14px}.quality-evidence{background:#f7f9f4;border-radius:8px;padding:16px;margin:14px 0;font-size:13px;line-height:1.9}.quality-evidence p{margin:8px 0 0}.quality-source,details{padding:12px 0;border-bottom:1px solid #e9ede5;font-size:12px;line-height:1.9;overflow-wrap:anywhere}.quality-source summary{cursor:pointer;color:#50704c}.quality-source p{margin:12px 0}.quality-review{margin-top:25px;padding-top:8px;border-top:1px solid #e9ede5}.quality-review .primary{margin-right:16px}.quality-detail pre{max-width:100%;overflow:auto}.quality-detail select{max-width:100%;text-overflow:ellipsis}@media(max-width:700px){.quality-options{gap:14px}.quality-options .primary{width:100%;justify-content:center}.quality-selects>label{flex-basis:100%}.quality-case .inline{flex-wrap:wrap}}
</style>
