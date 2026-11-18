<template>
  <section class="page" data-module="forecast_model">
    <header class="page-head">
      <div>
        <h2>预测模型</h2>
        <p class="page-desc">读取模型配置与历史观测，按稳定批次生成预测结果表；预测点从历史末端之后的第一个周期开始。</p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出预测结果表</button>
      </div>
    </header>

    <div class="analysis-summary">
      <article v-for="m in summaryMetrics" :key="m.label" class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">{{ m.label }}</span>
        <strong style="font-size:22px;display:block">{{ m.value }}</strong>
        <span style="font-size:12px;color:var(--muted)">按已落库批次统计</span>
      </article>
    </div>

    <form class="analysis-form" @submit.prevent="runAnalysis">
      <label>
        <span>模型编号</span>
        <select v-model="params.模型编号" data-testid="param-model">
          <option value="">按当前列表选择</option>
          <option v-for="item in configs" :key="item.id" :value="item.模型编号">{{ item.模型编号 }}</option>
        </select>
      </label>
      <label>
        <span>历史观测</span>
        <input v-model="params.历史观测" data-testid="param-history" placeholder="逗号分隔，例如 0.10,0.12,0.13" />
      </label>
      <label>
        <span>训练批次</span>
        <input v-model="params.训练批次" data-testid="param-batch" placeholder="留空自动生成" />
      </label>
      <label>
        <span>零斜率策略</span>
        <select v-model="params.零斜率策略" data-testid="param-strategy">
          <option value="">按模型参数配置</option>
          <option value="mean">统一取历史均值</option>
          <option value="last">保留最近观测值</option>
        </select>
      </label>
      <button class="btn primary" type="submit" data-testid="run-analysis" :disabled="loading">
        {{ loading ? '训练中…' : '运行分析' }}
      </button>
      <button class="btn ghost" type="button" data-testid="validate-results" @click="validateResults">按历史窗口校验</button>
    </form>

    <p v-if="message" :class="okFlag ? 'predict-hint' : 'error-text'" data-testid="train-message">{{ message }}</p>

    <div v-if="chartData.length" class="analysis-chart">
      <div v-for="(bar, i) in chartData" :key="i" class="analysis-bar" :style="{ height: barHeight(bar.value) + 'px' }">
        <span>{{ bar.label }}</span>
      </div>
    </div>

    <h3 class="section-title">模型配置与批次摘要</h3>
    <table class="data-table" data-testid="config-table">
      <thead>
        <tr>
          <th v-for="column in configColumns" :key="column">{{ column }}</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in configs" :key="String(row.id)" :data-status="String(row.结果状态)">
          <td v-for="column in configColumns" :key="column">{{ display(row, column) }}</td>
          <td class="row-actions">
            <button class="link" type="button" data-testid="open-detail" @click="openDetail(row)">详情</button>
            <button class="link" type="button" data-testid="train-model" @click="trainModel(row)">训练</button>
            <button class="link" type="button" data-testid="rollback-model" @click="rollback(row)">回滚</button>
            <button class="link" type="button" data-testid="archive-model" @click="loadArchive(row)">留档核对</button>
          </td>
        </tr>
        <tr v-if="!configs.length">
          <td :colspan="configColumns.length + 1" class="empty-state">暂无模型配置</td>
        </tr>
      </tbody>
    </table>

    <template v-if="activeResult">
      <h3 class="section-title">
        预测结果表
        <small class="predict-meta">
          批次 {{ activeResult.训练批次 || '—' }} · 历史窗口 {{ activeResult.历史窗口 }} ·
          周期 {{ activeResult.预测起点 }} 至 {{ activeResult.预测终点 }} · 版本 V{{ activeResult.版本号 }}
        </small>
      </h3>
      <p v-if="activeResult.说明" class="predict-hint" data-testid="result-note">{{ activeResult.说明 }}</p>
      <table class="data-table" data-testid="prediction-table">
        <thead>
          <tr><th v-for="column in resultColumns" :key="column">{{ column }}</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in activeResult.items" :key="`${item.指标标识}-${item.周期序号}`" data-testid="prediction-row">
            <td v-for="column in resultColumns" :key="column">{{ displayResult(item, column) }}</td>
          </tr>
          <tr v-if="!activeResult.items.length">
            <td :colspan="resultColumns.length" class="empty-state">该模型还没有已发布的预测结果</td>
          </tr>
        </tbody>
      </table>
    </template>
    <p v-else-if="!loading" style="text-align:center;color:var(--muted)">选择模型后运行分析，生成预测结果表</p>

    <section v-if="detail" class="predict-detail" data-testid="detail-panel">
      <header class="page-head">
        <div>
          <h3 class="section-title">模型详情 · {{ detail.模型编号 }}</h3>
          <p class="predict-meta">
            预测对象 {{ detail.预测对象 }} · 批次 {{ detail.result_summary?.最新批次 || '未训练' }} ·
            起点 {{ detail.预测起点 || '—' }} · 状态 {{ detail.结果状态 }}
          </p>
        </div>
        <button class="btn ghost" type="button" data-testid="close-detail" @click="detail = null">关闭详情</button>
      </header>
      <table class="data-table" data-testid="detail-prediction-table">
        <thead>
          <tr><th v-for="column in resultColumns" :key="column">{{ column }}</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in detail.predictions" :key="`${item.指标标识}-${item.周期序号}`">
            <td v-for="column in resultColumns" :key="column">{{ displayResult(item, column) }}</td>
          </tr>
          <tr v-if="!detail.predictions.length">
            <td :colspan="resultColumns.length" class="empty-state">详情页没有可展示的已发布结果</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-if="archive" class="predict-detail" data-testid="archive-panel">
      <h3 class="section-title">V030 留档核对 · {{ archive.模型编号 }}</h3>
      <table class="data-table">
        <thead>
          <tr><th v-for="column in archiveColumns" :key="column">{{ column }}</th></tr>
        </thead>
        <tbody>
          <tr v-for="record in archive.items" :key="record.批次号">
            <td v-for="column in archiveColumns" :key="column">{{ record[column] ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ configs.length }} 条模型配置</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/forecast_model'
const configColumns = ['模型编号', '预测对象', '模型类型', '历史窗口', '预测周期', '训练批次', '预测起点', '预测终点', '零斜率策略', '结果状态']
const resultColumns = ['模型编号', '指标标识', '训练批次', '预测周期', '历史窗口', '预测值', '预测类型', '边界标记', '说明']
const archiveColumns = ['批次号', '状态', 'active', '版本号', '历史窗口', '预测周期', '零斜率策略', '结果点数', '清理后剩余', '完整', '回滚原因']

const summaryMetrics = ref<{ label: string; value: number }[]>([])
const configs = ref<Row[]>([])
const activeResult = ref<Row | null>(null)
const detail = ref<Row | null>(null)
const archive = ref<Row | null>(null)
const loading = ref(false)
const message = ref('')
const okFlag = ref(true)
const errorMessage = ref('')
const params = reactive<Record<string, string>>({ 模型编号: '', 历史观测: '', 训练批次: '', 零斜率策略: '' })

const chartData = computed<{ label: string; value: number }[]>(() =>
  ((activeResult.value?.items ?? []) as Row[]).slice(0, 8).map((item: Row) => ({
    label: String(item.预测周期 ?? ''),
    value: Number(item.预测值 ?? 0),
  })),
)

function barHeight(value: number) {
  const values = chartData.value.map((item) => Math.abs(item.value))
  const max = Math.max(...values, 1)
  return Math.max(10, (Math.abs(value) / max) * 140)
}

function display(row: Row, column: string) {
  const value = row[column]
  if (column === '零斜率策略') return value === 'last' ? '保留最近观测值' : value === 'mean' ? '统一取历史均值' : '—'
  return value === '' || value === null || value === undefined ? '—' : value
}

function displayResult(item: Row, column: string) {
  const value = item[column]
  if (column === '零斜率' || column === '边界标记') return value || '—'
  return value === '' || value === null || value === undefined ? '—' : value
}

async function fetchJson(path: string, init?: RequestInit): Promise<Row> {
  const response = await request(path, init)
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return await response.json()
}

async function loadSummary() {
  try {
    const payload = await fetchJson(`${ENDPOINT}/summary`)
    summaryMetrics.value = payload.metrics ?? []
  } catch {
    summaryMetrics.value = []
  }
}

async function loadConfigs() {
  try {
    const payload = await fetchJson(ENDPOINT)
    configs.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '模型配置读取失败'
  }
}

async function loadResult(entryId: number) {
  try {
    activeResult.value = await fetchJson(`${ENDPOINT}/${entryId}/results`)
  } catch {
    activeResult.value = null
  }
}

function selectedConfig(): Row | null {
  if (params.模型编号) return configs.value.find((item) => item.模型编号 === params.模型编号) ?? null
  return configs.value[0] ?? null
}

async function runAnalysis() {
  loading.value = true
  message.value = ''
  errorMessage.value = ''
  try {
    const config = selectedConfig()
    const body: Row = { values: {} }
    if (config) body.values.模型编号 = config.模型编号
    if (params.历史观测) body.values.历史观测 = params.历史观测
    if (params.训练批次) body.values.训练批次 = params.训练批次
    if (params.零斜率策略) body.values.零斜率策略 = params.零斜率策略
    const payload = await fetchJson(`${ENDPOINT}/run`, { method: 'POST', body: JSON.stringify(body) })
    okFlag.value = Boolean(payload.ok)
    message.value = payload.message ?? ''
    if (payload.ok) {
      activeResult.value = payload
      await Promise.all([loadConfigs(), loadSummary()])
      if (detail.value && config) await openDetail((configs.value.find((item) => item.模型编号 === config.模型编号) ?? config))
    }
  } catch (error) {
    okFlag.value = false
    errorMessage.value = error instanceof Error ? error.message : '运行分析失败'
  } finally {
    loading.value = false
  }
}

async function trainModel(row: Row) {
  params.模型编号 = row.模型编号
  params.历史观测 = ''
  params.训练批次 = ''
  await loadResult(Number(row.id))
  await runAnalysis()
}

async function openDetail(row: Row) {
  try {
    detail.value = await fetchJson(`${ENDPOINT}/${row.id}`)
    await loadResult(Number(row.id))
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '模型详情读取失败'
  }
}

async function validateResults() {
  const config = selectedConfig()
  if (!config) return
  try {
    const payload = await fetchJson(`${ENDPOINT}/${config.id}/validate`)
    okFlag.value = Boolean(payload.通过)
    message.value = payload.通过
      ? `批次 ${payload.训练批次} 周期位置校验通过：${payload.预测起点} 至 ${payload.预测终点}`
      : `周期位置校验未通过：${(payload.问题 ?? []).join('；')}`
  } catch (error) {
    okFlag.value = false
    message.value = error instanceof Error ? error.message : '校验失败'
  }
}

async function rollback(row: Row) {
  try {
    const payload = await fetchJson(`${ENDPOINT}/${row.id}/rollback`, { method: 'POST' })
    okFlag.value = Boolean(payload.ok)
    message.value = payload.message ?? ''
    await loadResult(Number(row.id))
    await Promise.all([loadConfigs(), loadSummary()])
    if (detail.value) detail.value = await fetchJson(`${ENDPOINT}/${row.id}`)
  } catch (error) {
    okFlag.value = false
    message.value = error instanceof Error ? error.message : '回滚失败'
  }
}

async function loadArchive(row: Row) {
  try {
    archive.value = await fetchJson(`${ENDPOINT}/${row.id}/archive`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '留档读取失败'
  }
}

async function exportRows() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/export`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'forecast_model.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

onMounted(async () => {
  await Promise.all([loadSummary(), loadConfigs()])
  const first = configs.value[0]
  if (first) await loadResult(Number(first.id))
})
</script>

<style scoped>
.section-title { font-size: 15px; margin: 18px 0 8px; }
.section-title small { font-weight: 400; font-size: 12px; color: var(--muted); margin-left: 8px; }
.predict-meta { color: var(--muted); font-size: 12px; }
.predict-hint { color: #1f6feb; font-size: 12px; margin: 4px 0 8px; }
.predict-detail { margin-top: 18px; padding: 12px; background: #fff; border: 1px solid var(--border); border-radius: 8px; }
</style>
