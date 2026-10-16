<script setup lang="ts">
/**
 * `/history` — paginated, searchable history table with a shared audio player
 * (so only one track plays at a time), bulk deletion and a detail drawer.
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  Delete,
  Download,
  Headset,
  MagicStick,
  Plus,
  Refresh,
  Search,
  Star,
  StarFilled,
  View,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRouter } from 'vue-router'
import AudioPlayer from '@/components/AudioPlayer.vue'
import ErrorAlert from '@/components/ErrorAlert.vue'
import MetricsPanel from '@/components/MetricsPanel.vue'
import UsagePanel from '@/components/UsagePanel.vue'
import { ApiError } from '@/api/http'
import { audioUrl, deleteHistoryItem, fetchHistoryDetail, updateHistory } from '@/api'
import { MAX_TAG_COUNT, MAX_TAG_LENGTH, normalizeTags, sameTags } from '@/api/types'
import type { HistoryDetail, HistoryItem, TaskStatus } from '@/api/types'
import { useHistoryStore } from '@/stores/history'
import { useConfigStore } from '@/stores/config'
import { formatBytes, formatDateTime, formatSeconds, statusLabel, statusTagType, truncate } from '@/utils/format'
import { saveBlobAs } from '@/utils/download'
import {
  describeSummary,
  runBatchRetry,
  selectRetryable,
  summarizeResults,
  type RetryItemResult,
} from '@/utils/batchRetry'
import { pollUntilTerminal } from '@/utils/taskPolling'
import { withRateLimitRetry } from '@/utils/rateLimit'
import { submitTask } from '@/api'
import { zip } from 'fflate'

const historyStore = useHistoryStore()
const configStore = useConfigStore()
const router = useRouter()

/**
 * Copy a record's parameters into the config store.
 *
 * Shared by 复用此配置 and the batch retry so both restore a record the same
 * way. Note what is NOT copied: the API key. It was never stored with the
 * record (the backend keeps only a credential-stripped snapshot), so the
 * request is always rebuilt with the key the user currently holds.
 */
function applyRecordToConfig(row: HistoryItem): void {
  configStore.llmProviderId = row.llm_provider || configStore.llmProviderId
  if (row.llm_model) configStore.llmModel = row.llm_model
  configStore.ttsProviderId = row.tts_provider || configStore.ttsProviderId
  if (row.tts_voice) configStore.ttsVoice = row.tts_voice
  if (row.tts_model) configStore.ttsModel = row.tts_model
  if (row.audio_format) configStore.format = row.audio_format
  // The input text is what the user wants to tweak; the reply is available in
  // the drawer if they want the voiced version instead.
  configStore.text = row.input_text
}

/**
 * Fork a past generation back into the generate page: every parameter that
 * shaped the original run (providers, model, voice, speed, format, mode and
 * the text) is pushed into the config store, so "make a small change and
 * regenerate" is one click instead of retyping everything.
 */
function reuseConfig(row: HistoryItem): void {
  applyRecordToConfig(row)
  void router.push('/generate')
  ElMessage.success('已载入该记录的配置，可修改后重新生成')
}

/* ---------------------------- batch retry -------------------------------- */

const retrying = ref(false)
/** Set by the 停止 button while a batch is running. */
const retryStop = ref(false)
/** Items finished so far, for the progress line and the final summary. */
const retryDone = ref(0)
const retryTotal = ref(0)
/**
 * Set while waiting out a rate limit, so the pause is explained rather than
 * looking like a hang. A batch exceeds the 10-per-60s generate budget, so this
 * is expected on larger selections.
 */
const limitNote = ref('')

/** Selected records whose status warrants a retry. */
const retryableSelection = computed<HistoryItem[]>(() =>
  selectRetryable(historyStore.items.filter((item) => selectedIds.value.includes(item.id))),
)

/** Progress text while a batch runs, e.g. `第 3 / 8 项`. */
const retryProgressText = computed<string>(() =>
  retryTotal.value > 0 ? `第 ${Math.min(retryDone.value + 1, retryTotal.value)} / ${retryTotal.value} 项` : '',
)

/**
 * Re-run the selected failed / interrupted records.
 *
 * Runs here rather than bouncing to the generate page: the batch needs its own
 * progress, stop control and per-item summary, and the generate page owns a
 * single run. The requests are still built by `configStore.buildRequest()`, so
 * credentials keep flowing only from the local store.
 *
 * Sequential by design -- each item is a billable vendor call and the platform
 * is rate limited, so a selection of 20 must not become 20 simultaneous calls.
 */
async function retrySelected(): Promise<void> {
  const items = historyStore.items.filter((item) => selectedIds.value.includes(item.id))
  const eligible = selectRetryable(items)

  if (eligible.length === 0) {
    ElMessage.warning('选中的记录中没有失败或已中断的任务，无需重试')
    return
  }

  // Seed the store from the first record BEFORE checking eligibility. The
  // check is `canGenerate`, which also requires non-empty text and the
  // mode-specific fields -- and on a fresh page the composer is empty, so
  // checking first would reject a perfectly retryable batch.
  const first = eligible[0]
  if (first === undefined) return
  applyRecordToConfig(first)

  // Without credentials every item would fail identically, which is a worse
  // experience than being told once, up front.
  if (!configStore.canGenerate) {
    const reasons = configStore.disabledReasons
    ElMessage.error(
      reasons.length > 0
        ? `无法重试：${reasons.join('；')}`
        : '无法重试：请先在「系统设置」填写密钥与服务商',
    )
    return
  }

  const skipped = items.length - eligible.length
  const skippedNote = skipped > 0 ? `，另有 ${skipped} 条已完成将被跳过` : ''
  const confirmed = await ElMessageBox.confirm(
    `将依次重新生成 ${eligible.length} 条失败/已中断的记录${skippedNote}。` +
      '每一条都会真实调用服务商并可能产生费用，过程中可以随时停止。',
    '确认批量重试',
    { type: 'warning', confirmButtonText: '开始重试', cancelButtonText: '取消' },
  ).catch(() => false)

  if (confirmed === false) return

  retrying.value = true
  retryStop.value = false
  retryDone.value = 0
  retryTotal.value = items.length

  try {
    const results: RetryItemResult[] = await runBatchRetry(items, {
      shouldStop: () => retryStop.value,
      submit: async (item) => {
        // Rebuild from the record's own parameters, then submit through the
        // normal path so the request shape matches a manual generation exactly.
        //
        // Wrapped in rate-limit handling: a batch larger than the "generate"
        // budget (10 per 60s) exceeded it and failed part-way through -- seen
        // live as "请求过于频繁" on a 12-item run. Waiting for the server's own
        // retry_after turns those failures into successes instead of weakening
        // the limit.
        applyRecordToConfig(item)
        const submitted = await withRateLimitRetry(
          () => submitTask(configStore.buildRequest()),
          {
            shouldStop: () => retryStop.value,
            onWait: (waitMs) => {
              limitNote.value = `${Math.ceil(waitMs / 1000)} 秒后继续（触发限流，正在等待）`
            },
          },
        )
        limitNote.value = ''
        return submitted
      },
      poll: (taskId) => pollUntilTerminal(taskId, { shouldStop: () => retryStop.value }),
      onProgress: (_result, done) => {
        retryDone.value = done
      },
    })

    const summary = summarizeResults(results)
    const failures = results.filter((r) => r.outcome === 'failed')
    const detail =
      failures.length > 0
        ? `。失败原因：${failures
            .slice(0, 3)
            .map((f) => `${f.label}：${f.reason ?? '未知'}`)
            .join('；')}${failures.length > 3 ? ` 等 ${failures.length} 条` : ''}`
        : ''

    if (summary.failed > 0) {
      ElMessage.warning(describeSummary(summary) + detail)
    } else {
      ElMessage.success(describeSummary(summary))
    }

    // New records were created, so refresh the list.
    await historyStore.load()
    selectedIds.value = []
    historyStore.setSelection([])
  } catch (caught) {
    ElMessage.error(caught instanceof Error ? caught.message : '批量重试失败')
  } finally {
    retrying.value = false
    retryStop.value = false
    retryDone.value = 0
    retryTotal.value = 0
  }
}

/** Ask the running batch to stop before the next item is submitted. */
function stopRetry(): void {
  retryStop.value = true
  ElMessage.info('已请求停止：当前这一条完成后不再提交后续记录')
}

/* --------------------------- ZIP batch download -------------------------- */

const zipping = ref(false)

/**
 * Download all selected rows' audio as a single ZIP, client-side.
 * Each file is fetched individually, zipped with fflate (~2KB), and saved
 * via the browser's save-as dialog. File names include the record id so
 * they sort correctly.
 */
async function downloadSelectedAsZip(): Promise<void> {
  if (selectedIds.value.length === 0) return
  zipping.value = true
  try {
    const rows = historyStore.items.filter((item) => selectedIds.value.includes(item.id))
    const files: Record<string, Uint8Array> = {}
    for (const row of rows) {
      if (!row.audio_url) continue
      const response = await fetch(row.audio_url)
      if (!response.ok) continue
      const buffer = await response.arrayBuffer()
      const ext = row.audio_format || 'mp3'
      files[`text2voice-${row.id}.${ext}`] = new Uint8Array(buffer)
    }
    if (Object.keys(files).length === 0) {
      ElMessage.warning('选中的记录没有可下载的音频')
      return
    }
    const zipBytes = await new Promise<Uint8Array>((resolve, reject) => {
      zip(files, (err, data) => {
        if (err) reject(err)
        else resolve(data)
      })
    })
    saveBlobAs(new Blob([zipBytes], { type: 'application/zip' }), `text2voice-audio-${Date.now()}.zip`)
    ElMessage.success(`已打包 ${Object.keys(files).length} 个音频文件`)
  } catch (caught) {
    ElMessage.error(caught instanceof Error ? caught.message : '打包下载失败')
  } finally {
    zipping.value = false
  }
}

/* -------------------------------- loading -------------------------------- */

onMounted(() => {
  void historyStore.load()
})

/* -------------------------------- search --------------------------------- */

const searchInput = ref<string>(historyStore.query)
let searchTimer: ReturnType<typeof setTimeout> | null = null

/** Debounce typing by 400 ms before hitting the API. */
function handleSearchInput(value: string): void {
  searchInput.value = value
  if (searchTimer !== null) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    void historyStore.setQuery(value.trim())
  }, 400)
}

function clearSearch(): void {
  searchInput.value = ''
  if (searchTimer !== null) clearTimeout(searchTimer)
  void historyStore.setQuery('')
}

onBeforeUnmount(() => {
  if (searchTimer !== null) clearTimeout(searchTimer)
})

/** Status filter options. */
const statusOptions: { label: string; value: TaskStatus | '' }[] = [
  { label: '全部状态', value: '' },
  { label: '等待中', value: 'pending' },
  { label: '进行中', value: 'running' },
  { label: '成功', value: 'succeeded' },
  { label: '失败', value: 'failed' },
  { label: '已取消', value: 'canceled' },
  { label: '已中断', value: 'interrupted' },
]

const statusFilter = computed<TaskStatus | ''>({
  get: () => historyStore.status,
  set: (value: TaskStatus | '') => {
    void historyStore.setStatus(value)
  },
})

/* ------------------------- favorite / tag filters ------------------------ */

/**
 * Favourite filter. `''` is the "all" option and maps to `undefined` (no
 * filter) rather than `false`, which would mean "non-favourites only".
 */
const favoriteFilter = computed<'all' | 'only' | 'none'>({
  get: () => {
    if (historyStore.favorite === undefined) return 'all'
    return historyStore.favorite ? 'only' : 'none'
  },
  set: (value) => {
    void historyStore.setFavorite(value === 'all' ? undefined : value === 'only')
  },
})

const favoriteOptions = [
  { label: '全部记录', value: 'all' as const },
  { label: '仅看收藏', value: 'only' as const },
  { label: '仅看未收藏', value: 'none' as const },
]

/**
 * Tags available as filter chips: every distinct tag on the current page,
 * plus whatever is already selected (so an active chip never disappears just
 * because the filtered result set no longer contains it).
 */
const availableTags = computed<string[]>(() => {
  const seen = new Set<string>(historyStore.tags)
  for (const item of historyStore.items) {
    for (const tag of item.tags ?? []) seen.add(tag)
  }
  return [...seen].sort((left, right) => left.localeCompare(right, 'zh-Hans-CN'))
})

const activeTags = computed<string[]>(() => historyStore.tags)

function toggleTagFilter(tag: string): void {
  void historyStore.toggleTag(tag)
}

function clearTagFilter(): void {
  void historyStore.setTags([])
}

/* ------------------------------ star toggle ------------------------------ */

/** Record ids with an in-flight PATCH, so the star cannot be double-clicked. */
const starBusy = ref<Set<number>>(new Set())

async function toggleFavorite(row: HistoryItem): Promise<void> {
  if (starBusy.value.has(row.id)) return
  const next = !row.favorite
  starBusy.value = new Set(starBusy.value).add(row.id)
  // Optimistic: flip immediately, then reconcile with the server. The drawer
  // holds its own `HistoryDetail` object (a different instance from the store's
  // list row), so both are updated; otherwise the drawer button would keep
  // showing the stale label after a successful toggle.
  row.favorite = next
  historyStore.applyMetadata(row.id, { favorite: next })
  try {
    await updateHistory(row.id, { favorite: next })
    // Reload only when the active filter could now exclude the row, so the
    // visible list never contradicts the filter chip.
    if (historyStore.favorite !== undefined) await historyStore.load()
    ElMessage.success(next ? `已收藏记录 #${row.id}` : `已取消收藏 #${row.id}`)
  } catch (caught) {
    row.favorite = !next
    historyStore.applyMetadata(row.id, { favorite: !next }) // roll back
    ElMessage.error(caught instanceof ApiError ? caught.message : '更新收藏失败')
  } finally {
    const nextBusy = new Set(starBusy.value)
    nextBusy.delete(row.id)
    starBusy.value = nextBusy
  }
}

/* ------------------------------- selection ------------------------------- */

/** Ids selected via the table's checkbox column. */
const selectedIds = ref<number[]>([])

function handleSelectionChange(rows: HistoryItem[]): void {
  selectedIds.value = rows.map((row) => row.id)
  historyStore.setSelection(selectedIds.value)
}

/* ------------------------------ shared audio ----------------------------- */

const current = ref<HistoryItem | null>(null)
const playerVisible = ref(false)
const playerRef = ref<InstanceType<typeof AudioPlayer> | null>(null)

/** Currently playing record, or `null` when the bar is hidden. */
const playerSrc = computed<string>(() =>
  current.value ? current.value.audio_url || audioUrl(current.value.id) : '',
)

const playerTitle = computed<string>(() =>
  current.value ? `#${current.value.id} · ${truncate(current.value.reply_text, 40)}` : '',
)

const playerDownloadUrl = computed<string>(() => {
  if (!current.value) return ''
  const base = current.value.audio_url || audioUrl(current.value.id)
  return base.includes('?') ? `${base}&download=1` : `${base}?download=1`
})

const playerFormat = computed<string>(() => current.value?.audio_format ?? 'mp3')
const playerDuration = computed<number | null>(() => current.value?.duration_ms ?? null)

/** Play a row in the shared player, replacing whatever was playing. */
async function playRow(row: HistoryItem): Promise<void> {
  if (!row.audio_url && !row.id) {
    ElMessage.warning('该记录没有可播放的音频')
    return
  }
  if (current.value?.id === row.id) {
    playerRef.value?.toggle()
    return
  }
  current.value = row
  playerVisible.value = true
  // The player watches `src`; wait for the re-render before starting playback.
  await nextTick()
  playerRef.value?.play()
}

function closePlayer(): void {
  playerRef.value?.stop()
  playerVisible.value = false
  current.value = null
}

/* --------------------------------- detail -------------------------------- */

const drawerVisible = ref(false)
const detailLoading = ref(false)
const detail = ref<HistoryDetail | null>(null)
const detailError = ref<ApiError | null>(null)

async function openDetail(row: HistoryItem): Promise<void> {
  drawerVisible.value = true
  detailLoading.value = true
  detailError.value = null
  detail.value = null
  try {
    detail.value = await fetchHistoryDetail(row.id)
  } catch (caught) {
    detailError.value = caught instanceof ApiError ? caught : null
    if (!detailError.value) ElMessage.error('加载详情失败')
  } finally {
    detailLoading.value = false
  }
}

/** The drawer falls back to the list row while the detail request is in flight. */
const drawerItem = computed<HistoryItem | null>(() => detail.value ?? null)

/* ---------------------------- drawer tag editor --------------------------- */

/** Working copy of the drawer row's tags; committed by 保存标签. */
const tagDraft = ref<string[]>([])
const tagInput = ref('')
const tagSaving = ref(false)

/** Re-seed the draft whenever a different record is loaded. */
watch(
  () => detail.value?.id,
  () => {
    tagDraft.value = [...(detail.value?.tags ?? [])]
    tagInput.value = ''
  },
)

/** Local validation feedback, mirroring the backend's rules. */
const tagHint = computed<string>(() => {
  if (tagDraft.value.length >= MAX_TAG_COUNT) return `最多 ${MAX_TAG_COUNT} 个标签`
  return `最多 ${MAX_TAG_COUNT} 个标签，每个不超过 ${MAX_TAG_LENGTH} 个字符`
})

const tagDirty = computed<boolean>(() => !sameTags(tagDraft.value, detail.value?.tags ?? []))

function addTag(): void {
  const candidate = tagInput.value.trim()
  if (!candidate) return
  if (candidate.length > MAX_TAG_LENGTH) {
    ElMessage.warning(`单个标签最长 ${MAX_TAG_LENGTH} 个字符`)
    return
  }
  if (tagDraft.value.includes(candidate)) {
    ElMessage.info('该标签已存在')
    tagInput.value = ''
    return
  }
  if (tagDraft.value.length >= MAX_TAG_COUNT) {
    ElMessage.warning(`最多 ${MAX_TAG_COUNT} 个标签`)
    return
  }
  tagDraft.value = [...tagDraft.value, candidate]
  tagInput.value = ''
}

function removeTag(tag: string): void {
  tagDraft.value = tagDraft.value.filter((item) => item !== tag)
}

async function saveTags(): Promise<void> {
  const target = detail.value
  if (!target) return
  const next = normalizeTags(tagDraft.value)
  tagSaving.value = true
  try {
    const updated = await updateHistory(target.id, { tags: next })
    detail.value = updated
    historyStore.applyMetadata(target.id, { tags: updated.tags })
    tagDraft.value = [...updated.tags]
    ElMessage.success('标签已保存')
    // Tags are a filter dimension, so refresh when one is active.
    if (historyStore.tags.length > 0) await historyStore.load()
  } catch (caught) {
    ElMessage.error(caught instanceof ApiError ? caught.message : '保存标签失败')
  } finally {
    tagSaving.value = false
  }
}

/* --------------------------------- delete -------------------------------- */

const deleting = ref(false)

async function removeRow(row: HistoryItem): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定要删除记录 #${row.id} 吗？该操作不可恢复。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await deleteHistoryItem(row.id)
    if (current.value?.id === row.id) closePlayer()
    ElMessage.success(`已删除记录 #${row.id}`)
    await historyStore.load()
  } catch (caught) {
    ElMessage.error(caught instanceof ApiError ? caught.message : '删除失败')
  }
}

async function removeSelected(): Promise<void> {
  if (selectedIds.value.length === 0) {
    ElMessage.warning('请先选择要删除的记录')
    return
  }
  try {
    await ElMessageBox.confirm(
      `确定要删除选中的 ${selectedIds.value.length} 条记录吗？该操作不可恢复。`,
      '批量删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  deleting.value = true
  try {
    const deleted = await historyStore.removeMany([...selectedIds.value])
    ElMessage.success(`已删除 ${deleted} 条记录`)
    selectedIds.value = []
  } catch (caught) {
    ElMessage.error(caught instanceof ApiError ? caught.message : '批量删除失败')
  } finally {
    deleting.value = false
  }
}

async function removeAll(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '确定要清空全部历史记录吗？该操作不可恢复。',
      '清空确认',
      { type: 'warning', confirmButtonText: '全部删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  deleting.value = true
  try {
    const deleted = await historyStore.removeAll()
    selectedIds.value = []
    closePlayer()
    ElMessage.success(`已删除 ${deleted} 条记录`)
  } catch (caught) {
    ElMessage.error(caught instanceof ApiError ? caught.message : '清空失败')
  } finally {
    deleting.value = false
  }
}

/* ------------------------------- pagination ------------------------------ */

const pageSizes = [10, 20, 50]

const currentPage = computed<number>({
  get: () => historyStore.page,
  set: (value: number) => {
    void historyStore.setPage(value)
  },
})

const pageSize = computed<number>({
  get: () => historyStore.pageSize,
  set: (value: number) => {
    void historyStore.setPageSize(value)
  },
})

/** Row index offset so the 序号 column is continuous across pages. */
function rowIndex(index: number): number {
  return (historyStore.page - 1) * historyStore.pageSize + index + 1
}

/** Store error promoted to an `ApiError` so `ErrorAlert` can render it. */
const historyError = computed<ApiError | null>(() =>
  historyStore.error ? new ApiError({ code: 'HISTORY_LOAD_FAILED', message: historyStore.error }) : null,
)

function notifyDownload(): void {
  ElMessage.success('已开始下载音频文件')
}

/* --------------------------- drawer audio player -------------------------- */

const drawerPlayerRef = ref<InstanceType<typeof AudioPlayer> | null>(null)

/** Pause the sticky player when the drawer opens to avoid double playback. */
watch(drawerVisible, (open) => {
  if (open) playerRef.value?.pause()
})
</script>

<template>
  <div class="t2v-page history-view">
    <div class="t2v-page__title">
      <h2>历史记录</h2>
      <span class="t2v-page__subtitle">共 {{ historyStore.total }} 条记录</span>
    </div>

    <el-card shadow="never" class="history-view__toolbar-card">
      <div class="history-view__toolbar">
        <div class="history-view__toolbar-left">
          <el-input
            :model-value="searchInput"
            class="history-view__search"
            placeholder="搜索输入文本或回复内容"
            clearable
            :prefix-icon="Search"
            @update:model-value="handleSearchInput"
            @clear="clearSearch"
          />
          <el-select v-model="statusFilter" class="history-view__status" placeholder="状态筛选">
            <el-option
              v-for="item in statusOptions"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
          <el-select v-model="favoriteFilter" class="history-view__favorite" placeholder="收藏筛选">
            <el-option
              v-for="item in favoriteOptions"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </div>

        <div class="history-view__toolbar-right">
          <el-button :icon="Refresh" :loading="historyStore.loading" @click="historyStore.load()">
            刷新
          </el-button>
          <el-button
            type="danger"
            plain
            :icon="Delete"
            :disabled="selectedIds.length === 0"
            :loading="deleting"
            @click="removeSelected"
          >
            批量删除{{ selectedIds.length > 0 ? `（${selectedIds.length}）` : '' }}
          </el-button>
          <el-button
            type="primary"
            plain
            :icon="Download"
            :disabled="selectedIds.length === 0"
            :loading="zipping"
            @click="downloadSelectedAsZip"
          >
            打包下载{{ selectedIds.length > 0 ? `（${selectedIds.length}）` : '' }}
          </el-button>
          <el-button
            type="warning"
            plain
            :icon="Refresh"
            :disabled="selectedIds.length === 0 || retrying"
            :loading="retrying"
            @click="retrySelected"
          >
            重试失败项{{ retryableSelection.length > 0 ? `（${retryableSelection.length}）` : '' }}
          </el-button>
          <el-button
            v-if="retrying"
            type="info"
            plain
            @click="stopRetry"
          >
            停止
          </el-button>
          <el-button type="danger" plain :icon="Delete" :loading="deleting" @click="removeAll">
            清空全部
          </el-button>
        </div>
      </div>

      <el-alert
        v-if="retrying"
        class="history-view__retry-progress"
        type="info"
        :closable="false"
        show-icon
      >
        <template #title>
          正在依次重试{{ retryProgressText ? `：${retryProgressText}` : '' }}（已完成
          {{ retryDone }} / {{ retryTotal }}）。<template v-if="limitNote">{{ limitNote }}。</template
          ><template v-else>每条都会真实调用服务商，可点「停止」中止。</template>
        </template>
      </el-alert>

      <div v-if="availableTags.length > 0" class="history-view__tag-filter">
        <span class="history-view__tag-filter-label">标签</span>
        <el-check-tag
          v-for="tag in availableTags"
          :key="tag"
          :checked="activeTags.includes(tag)"
          class="history-view__tag-chip"
          @change="toggleTagFilter(tag)"
        >
          {{ tag }}
        </el-check-tag>
        <el-button
          v-if="activeTags.length > 0"
          link
          type="primary"
          size="small"
          @click="clearTagFilter"
        >
          清除标签筛选
        </el-button>
      </div>
    </el-card>

    <ErrorAlert
      v-if="historyError"
      :error="historyError"
      title="加载历史记录失败"
      :copyable="false"
      @retry="historyStore.load()"
      @dismiss="historyStore.error = null"
    />

    <UsagePanel ref="usagePanelRef" />

    <MetricsPanel ref="metricsPanelRef" />

    <el-card shadow="never" class="history-view__table-card">
      <el-table
        v-loading="historyStore.loading"
        :data="historyStore.items"
        row-key="id"
        stripe
        border
        class="history-view__table"
        @selection-change="handleSelectionChange"
      >
        <el-table-column type="selection" width="46" reserve-selection />

        <el-table-column label="序号" width="70" align="center">
          <template #default="{ $index }">
            <span class="t2v-mono">{{ rowIndex($index) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="收藏" width="70" align="center">
          <template #default="{ row }">
            <el-tooltip :content="row.favorite ? '取消收藏' : '收藏'" placement="top">
              <el-button
                size="small"
                circle
                :type="row.favorite ? 'warning' : 'default'"
                :icon="row.favorite ? StarFilled : Star"
                :loading="starBusy.has(row.id)"
                :aria-label="row.favorite ? '取消收藏' : '收藏'"
                @click="toggleFavorite(row)"
              />
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="标签" min-width="140">
          <template #default="{ row }">
            <div v-if="row.tags && row.tags.length > 0" class="history-view__row-tags">
              <el-tag
                v-for="tag in row.tags"
                :key="tag"
                size="small"
                effect="plain"
                class="history-view__row-tag"
                @click="toggleTagFilter(tag)"
              >
                {{ tag }}
              </el-tag>
            </div>
            <span v-else class="history-view__muted">—</span>
          </template>
        </el-table-column>

        <el-table-column label="输入文本" min-width="200">
          <template #default="{ row }">
            <el-tooltip :content="row.input_text" placement="top" :show-after="300">
              <span class="history-view__cell-text">{{ truncate(row.input_text, 48) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="回复文本" min-width="220">
          <template #default="{ row }">
            <el-tooltip :content="row.reply_text" placement="top" :show-after="300">
              <span class="history-view__cell-text">{{ truncate(row.reply_text, 56) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column label="服务商" width="200">
          <template #default="{ row }">
            <div class="history-view__providers">
              <el-tag size="small" effect="plain" type="primary">
                LLM · {{ row.llm_provider }}/{{ row.llm_model }}
              </el-tag>
              <el-tag size="small" effect="plain" type="success">
                TTS · {{ row.tts_provider }}/{{ row.tts_voice }}
              </el-tag>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="时长" width="90" align="right">
          <template #default="{ row }">
            <span class="t2v-mono">{{ formatSeconds(row.duration_ms) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="大小" width="90" align="right">
          <template #default="{ row }">
            <span class="t2v-mono">{{ formatBytes(row.audio_bytes) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="110" align="center">
          <template #default="{ row }">
            <div class="history-view__status-cell">
              <el-tag :type="statusTagType(row.status)" size="small" effect="light">
                {{ statusLabel(row.status) }}
              </el-tag>
              <el-tag v-if="row.degraded" type="warning" size="small" effect="plain">降级</el-tag>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">
            <span class="t2v-mono">{{ formatDateTime(row.created_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="270" fixed="right">
          <template #default="{ row }">
            <div class="history-view__actions">
              <el-tooltip content="播放" placement="top">
                <el-button
                  size="small"
                  :icon="Headset"
                  circle
                  :disabled="!row.audio_url"
                  aria-label="播放"
                  @click="playRow(row)"
                />
              </el-tooltip>
              <el-tooltip content="下载音频" placement="top">
                <el-button
                  size="small"
                  :icon="Download"
                  circle
                  tag="a"
                  :href="row.audio_url ? `${row.audio_url}?download=1` : undefined"
                  :download="`text2voice-${row.id}.${row.audio_format || 'mp3'}`"
                  :disabled="!row.audio_url"
                  aria-label="下载音频"
                  @click="notifyDownload"
                />
              </el-tooltip>
              <el-tooltip content="载入此配置到生成页" placement="top">
                <el-button
                  size="small"
                  :icon="MagicStick"
                  circle
                  type="primary"
                  plain
                  aria-label="复用此配置"
                  @click="reuseConfig(row)"
                />
              </el-tooltip>
              <el-tooltip content="详情" placement="top">
                <el-button size="small" :icon="View" circle aria-label="查看详情" @click="openDetail(row)" />
              </el-tooltip>
              <el-tooltip content="删除" placement="top">
                <el-button
                  size="small"
                  type="danger"
                  :icon="Delete"
                  circle
                  plain
                  aria-label="删除记录"
                  @click="removeRow(row)"
                />
              </el-tooltip>
            </div>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty description="暂无历史记录" :image-size="80" />
        </template>
      </el-table>

      <div class="history-view__pagination">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :page-sizes="pageSizes"
          :total="historyStore.total"
          layout="total, sizes, prev, pager, next, jumper"
          background
        />
      </div>
    </el-card>

    <!-- --------------------------- sticky player --------------------------- -->
    <transition name="t2v-slide-up">
      <div v-if="playerVisible" class="history-view__player-bar">
        <div class="history-view__player-info">
          <el-tag size="small" effect="plain" type="info" class="t2v-mono">
            #{{ current?.id }}
          </el-tag>
          <span class="history-view__player-title">{{ playerTitle }}</span>
        </div>
        <AudioPlayer
          ref="playerRef"
          class="history-view__player"
          :src="playerSrc"
          :download-url="playerDownloadUrl"
          :download-name="`text2voice-${current?.id ?? 'audio'}.${playerFormat}`"
          :duration-ms="playerDuration"
          :format="playerFormat"
          compact
        />
        <el-button size="small" @click="closePlayer">关闭</el-button>
      </div>
    </transition>

    <!-- ------------------------------ drawer ------------------------------ -->
    <el-drawer v-model="drawerVisible" title="记录详情" size="560px" :destroy-on-close="true">
      <div v-loading="detailLoading" class="history-view__drawer">
        <ErrorAlert
          v-if="detailError"
          :error="detailError"
          title="加载详情失败"
          :retryable="false"
        />

        <template v-if="drawerItem">
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="记录 ID">
              <span class="t2v-mono">#{{ drawerItem.id }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="状态">
              <el-tag :type="statusTagType(drawerItem.status)" size="small">
                {{ statusLabel(drawerItem.status) }}
              </el-tag>
              <el-tag v-if="drawerItem.degraded" type="warning" size="small" effect="plain">
                已降级
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="创建时间">
              {{ formatDateTime(drawerItem.created_at) }}
            </el-descriptions-item>
            <el-descriptions-item label="LLM">
              {{ drawerItem.llm_provider }} / {{ drawerItem.llm_model }}
            </el-descriptions-item>
            <el-descriptions-item label="TTS">
              {{ drawerItem.tts_provider }} / {{ drawerItem.tts_voice }}
            </el-descriptions-item>
            <el-descriptions-item label="音频">
              {{ drawerItem.audio_format?.toUpperCase() }} ·
              {{ formatBytes(drawerItem.audio_bytes) }} ·
              {{ formatSeconds(drawerItem.duration_ms) }}
            </el-descriptions-item>
            <el-descriptions-item v-if="detail" label="总耗时">
              {{ formatSeconds(detail.total_latency_ms) }}
            </el-descriptions-item>
            <el-descriptions-item v-if="detail" label="LLM / TTS 耗时">
              {{ formatSeconds(detail.llm_latency_ms) }} / {{ formatSeconds(detail.tts_latency_ms) }}
            </el-descriptions-item>
            <el-descriptions-item v-if="drawerItem.error_code" label="错误码">
              <span class="t2v-mono">{{ drawerItem.error_code }}</span>
            </el-descriptions-item>
            <el-descriptions-item v-if="detail?.error_message" label="错误信息">
              {{ detail.error_message }}
            </el-descriptions-item>
          </el-descriptions>

          <section class="history-view__drawer-section">
            <h4>收藏与标签</h4>
            <div class="history-view__tag-editor">
              <el-button
                :type="drawerItem.favorite ? 'warning' : 'default'"
                :icon="drawerItem.favorite ? StarFilled : Star"
                size="small"
                @click="toggleFavorite(drawerItem)"
              >
                {{ drawerItem.favorite ? '已收藏' : '收藏此记录' }}
              </el-button>

              <div class="history-view__tag-draft">
                <el-tag
                  v-for="tag in tagDraft"
                  :key="tag"
                  closable
                  size="small"
                  type="info"
                  @close="removeTag(tag)"
                >
                  {{ tag }}
                </el-tag>
                <span v-if="tagDraft.length === 0" class="history-view__muted">暂无标签</span>
              </div>

              <div class="history-view__tag-input">
                <el-input
                  v-model="tagInput"
                  size="small"
                  :maxlength="MAX_TAG_LENGTH"
                  placeholder="输入标签后回车添加"
                  @keyup.enter="addTag"
                />
                <el-button size="small" :icon="Plus" @click="addTag">添加</el-button>
              </div>
              <p class="history-view__tag-hint">{{ tagHint }}</p>
              <el-button
                type="primary"
                size="small"
                :loading="tagSaving"
                :disabled="!tagDirty"
                @click="saveTags"
              >
                保存标签
              </el-button>
            </div>
          </section>

          <section class="history-view__drawer-section">
            <h4>输入文本</h4>
            <el-scrollbar max-height="180">
              <p class="t2v-scroll-text t2v-selectable">{{ drawerItem.input_text }}</p>
            </el-scrollbar>
          </section>

          <section class="history-view__drawer-section">
            <h4>回复文本</h4>
            <el-scrollbar max-height="260">
              <p class="t2v-scroll-text t2v-selectable">{{ drawerItem.reply_text }}</p>
            </el-scrollbar>
          </section>

          <section v-if="drawerItem.audio_url" class="history-view__drawer-section">
            <h4>音频</h4>
            <AudioPlayer
              ref="drawerPlayerRef"
              :src="drawerItem.audio_url"
              :download-url="`${drawerItem.audio_url}?download=1`"
              :download-name="`text2voice-${drawerItem.id}.${drawerItem.audio_format || 'mp3'}`"
              :duration-ms="drawerItem.duration_ms"
              :format="drawerItem.audio_format"
              title="历史音频"
            />
          </section>

          <div class="history-view__drawer-actions">
            <el-button type="primary" :icon="MagicStick" @click="reuseConfig(drawerItem)">
              载入此配置到生成页
            </el-button>
            <el-button @click="drawerVisible = false">关闭</el-button>
          </div>
        </template>

        <el-empty v-else-if="!detailLoading && !detailError" description="暂无详情" />
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.history-view__toolbar-card :deep(.el-card__body) {
  padding: 14px 18px;
}

.history-view__toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.history-view__toolbar-left,
.history-view__toolbar-right {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.history-view__search {
  width: 280px;
}

.history-view__status {
  width: 150px;
}

.history-view__favorite {
  width: 140px;
}

/* ---------------------------- tag filter row ----------------------------- */

.history-view__tag-filter {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px dashed var(--t2v-border-color);
}

.history-view__tag-filter-label {
  font-size: 13px;
  color: var(--t2v-text-secondary);
}

.history-view__tag-chip {
  cursor: pointer;
}

/* ------------------------------ table cells ------------------------------ */

.history-view__row-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.history-view__row-tag {
  cursor: pointer;
}

.history-view__muted {
  color: var(--t2v-text-secondary);
}

/* ----------------------------- drawer editor ----------------------------- */

.history-view__tag-editor {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  background: var(--t2v-bg-muted);
  border: 1px solid var(--t2v-border-color);
  border-radius: var(--t2v-radius-sm);
}

.history-view__tag-draft {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  min-height: 24px;
  align-items: center;
}

.history-view__tag-input {
  display: flex;
  gap: 8px;
}

.history-view__tag-hint {
  margin: 0;
  padding: 0;
  background: none;
  border: none;
  font-size: 12px;
  color: var(--t2v-text-secondary);
}

.history-view__table-card :deep(.el-card__body) {
  padding: 0;
}

.history-view__table {
  width: 100%;
}

.history-view__cell-text {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: middle;
}

.history-view__providers {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: flex-start;
}

.history-view__status-cell {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: center;
}

.history-view__actions {
  display: flex;
  align-items: center;
  gap: 6px;
}

.history-view__pagination {
  display: flex;
  justify-content: flex-end;
  padding: 14px 18px;
  border-top: 1px solid var(--t2v-border-color);
}

.history-view__player-bar {
  position: sticky;
  bottom: 0;
  z-index: 12;
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 10px 16px;
  background: var(--t2v-bg-surface);
  border: 1px solid var(--t2v-border-color);
  border-radius: var(--t2v-radius-md);
  box-shadow: var(--t2v-shadow-md);
}

.history-view__player-info {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 0 0 auto;
  max-width: 260px;
}

.history-view__player-title {
  font-size: 13px;
  color: var(--t2v-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.history-view__player {
  flex: 1 1 auto;
  min-width: 0;
}

.history-view__drawer {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.history-view__drawer-section h4 {
  margin: 0 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--t2v-text-secondary);
}

.history-view__drawer-section p {
  padding: 10px 12px;
  background: var(--t2v-bg-muted);
  border: 1px solid var(--t2v-border-color);
  border-radius: var(--t2v-radius-sm);
}

.history-view__drawer-actions {
  display: flex;
  gap: 10px;
  margin-top: 20px;
  padding-top: 16px;
  border-top: 1px solid var(--t2v-border-color);
}

.t2v-slide-up-enter-active,
.t2v-slide-up-leave-active {
  transition: opacity 0.2s ease, transform 0.2s ease;
}

.t2v-slide-up-enter-from,
.t2v-slide-up-leave-to {
  opacity: 0;
  transform: translateY(12px);
}

@media (max-width: 1024px) {
  .history-view__search {
    width: 200px;
  }

  .history-view__player-info {
    display: none;
  }
}
</style>