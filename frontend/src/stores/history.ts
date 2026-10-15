/**
 * History store: paginated list, search query and selection state.
 */
import { defineStore } from 'pinia'
import { deleteHistoryBulk, deleteHistoryItem, fetchHistory } from '@/api'
import type { HistoryItem, TaskStatus } from '@/api/types'

export interface HistoryState {
  items: HistoryItem[]
  total: number
  page: number
  pageSize: number
  pages: number
  /** Debounced free-text query mapped to the `q` parameter. */
  query: string
  /** Status filter; empty string means "all". */
  status: TaskStatus | ''
  /**
   * Favourite filter: `undefined` = no filter, `true` = favourites only,
   * `false` = non-favourites only. `undefined` is distinct from `false`
   * because "no favourite filter" must not mean "hide favourites".
   */
  favorite?: boolean
  /** Active tag filter (OR semantics); empty array = no tag filter. */
  tags: string[]
  loading: boolean
  error: string | null
  /** Row ids currently selected in the table. */
  selection: number[]
}

export const useHistoryStore = defineStore('history', {
  state: (): HistoryState => ({
    items: [],
    total: 0,
    page: 1,
    pageSize: 20,
    pages: 1,
    query: '',
    status: '',
    favorite: undefined,
    tags: [],
    loading: false,
    error: null,
    selection: [],
  }),

  getters: {
    /** Whether the current page has any rows. */
    isEmpty(state): boolean {
      return !state.loading && state.items.length === 0
    },

    /** Number of rows selected for bulk deletion. */
    selectedCount(state): number {
      return state.selection.length
    },
  },

  actions: {
    /** `GET /history` with the current query state. */
    async load(): Promise<void> {
      this.loading = true
      this.error = null
      try {
        const response = await fetchHistory({
          page: this.page,
          page_size: this.pageSize,
          q: this.query,
          status: this.status,
          favorite: this.favorite,
          tags: this.tags,
        })
        this.items = response.items
        this.total = response.total
        this.page = response.page
        this.pageSize = response.page_size
        this.pages = response.pages
        // Drop selections that no longer exist on this page.
        const visible = new Set(this.items.map((item) => item.id))
        this.selection = this.selection.filter((id) => visible.has(id))
      } catch (error) {
        this.error = error instanceof Error ? error.message : '加载历史记录失败'
        this.items = []
        this.total = 0
        this.pages = 1
      } finally {
        this.loading = false
      }
    },

    /** Change page size and reload from page 1. */
    async setPageSize(size: number): Promise<void> {
      this.pageSize = size
      this.page = 1
      await this.load()
    },

    /** Jump to a page and reload. */
    async setPage(page: number): Promise<void> {
      this.page = page
      await this.load()
    },

    /** Apply a new search term (already debounced by the view). */
    async setQuery(query: string): Promise<void> {
      this.query = query
      this.page = 1
      await this.load()
    },

    /** Apply a status filter and reload from page 1. */
    async setStatus(status: TaskStatus | ''): Promise<void> {
      this.status = status
      this.page = 1
      await this.load()
    },

    /**
     * Apply the favourite filter and reload from page 1.
     * Pass `undefined` for "all records".
     */
    async setFavorite(favorite: boolean | undefined): Promise<void> {
      this.favorite = favorite
      this.page = 1
      await this.load()
    },

    /** Apply a tag filter (OR semantics) and reload from page 1. */
    async setTags(tags: string[]): Promise<void> {
      this.tags = [...tags]
      this.page = 1
      await this.load()
    },

    /** Add or remove one tag from the active filter. */
    async toggleTag(tag: string): Promise<void> {
      const next = this.tags.includes(tag)
        ? this.tags.filter((item) => item !== tag)
        : [...this.tags, tag]
      await this.setTags(next)
    },

    /**
     * Update one row's metadata in place after a successful `PATCH`.
     *
     * Avoids a full reload so the page, scroll position and selection stay put
     * when the user only clicked a star.
     */
    applyMetadata(
      id: number,
      patch: { favorite?: boolean; tags?: string[] },
    ): void {
      const row = this.items.find((item) => item.id === id)
      if (!row) return
      if (patch.favorite !== undefined) row.favorite = patch.favorite
      if (patch.tags !== undefined) row.tags = [...patch.tags]
    },

    /** Replace a row's metadata and reload (used when a filter may exclude it). */
    async applyMetadataAndReload(
      id: number,
      patch: { favorite?: boolean; tags?: string[] },
    ): Promise<void> {
      this.applyMetadata(id, patch)
      // A row can drop out of the current filtered view (e.g. un-starring while
      // the "favourites only" filter is active), so re-fetch to stay truthful.
      await this.load()
    },

    /** Replace the row selection. */
    setSelection(ids: number[]): void {
      this.selection = ids
    },

    /** `DELETE /history/{id}` then refresh the current page. */
    async remove(id: number): Promise<number> {
      await deleteHistoryItem(id)
      this.selection = this.selection.filter((item) => item !== id)
      // Step back a page when the last row of the last page was removed.
      if (this.items.length === 1 && this.page > 1) this.page -= 1
      await this.load()
      return id
    },

    /** `DELETE /history` with `{ ids }`. */
    async removeMany(ids: number[]): Promise<number> {
      const response = await deleteHistoryBulk({ ids })
      this.selection = []
      await this.load()
      return response.deleted
    },

    /** `DELETE /history` with `{ all: true }`. */
    async removeAll(): Promise<number> {
      const response = await deleteHistoryBulk({ all: true })
      this.selection = []
      this.page = 1
      await this.load()
      return response.deleted
    },

    /** Clear local state (used when leaving the page is not desired). */
    reset(): void {
      this.items = []
      this.total = 0
      this.page = 1
      this.pages = 1
      this.query = ''
      this.status = ''
      this.favorite = undefined
      this.tags = []
      this.selection = []
      this.error = null
    },
  },
})