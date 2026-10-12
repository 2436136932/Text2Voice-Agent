/**
 * Browser-side file export.
 *
 * The user asked for a "另存为" (save as) experience where they pick the
 * destination themselves. Browsers expose that through the File System Access
 * API (`showSaveFilePicker`, Chromium 86+). Where it is unavailable
 * (Firefox/Safari, or a non-secure context) we fall back to an `<a download>`
 * click, which lands in the browser's configured download folder.
 *
 * Nothing here touches the server: the audio bytes come from the API and the
 * chosen path never leaves the browser.
 */

/** Suggested filename for a generated clip. */
export function buildAudioFilename(
  recordId: number | string,
  extension: string,
  prefix = 'text2voice',
): string {
  const ext = (extension || 'mp3').replace(/^\./, '')
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
  return `${prefix}_${recordId}_${stamp}.${ext}`
}

interface SavePickerOptions {
  suggestedName?: string
  types?: { description: string; accept: Record<string, string[]> }[]
}

interface SaveFileHandle {
  createWritable(): Promise<{ write(data: Blob): Promise<void>; close(): Promise<void> }>
}

type SavePicker = (options: SavePickerOptions) => Promise<SaveFileHandle>

/** True when the browser can prompt for a destination path. */
export function canPickSaveLocation(): boolean {
  return typeof (window as unknown as { showSaveFilePicker?: unknown }).showSaveFilePicker === 'function'
}

export interface SaveResult {
  /** `picker` = user chose a path; `download` = fell back to the download folder. */
  mode: 'picker' | 'download'
  /** True when the user dismissed the save dialog. */
  cancelled: boolean
  filename: string
}

/**
 * Save a blob, preferring a user-chosen destination.
 *
 * @param blob     content to write
 * @param filename suggested name (also used for the fallback download)
 * @param mimeType used to build the picker's file-type filter
 */
export async function saveBlobAs(
  blob: Blob,
  filename: string,
  mimeType = 'audio/mpeg',
): Promise<SaveResult> {
  const picker = (window as unknown as { showSaveFilePicker?: SavePicker }).showSaveFilePicker

  if (typeof picker === 'function') {
    const extension = filename.includes('.') ? `.${filename.split('.').pop()}` : ''
    try {
      const handle = await picker({
        suggestedName: filename,
        types: [
          {
            description: '音频文件',
            accept: { [mimeType]: extension ? [extension] : ['.mp3'] },
          },
        ],
      })
      const writable = await handle.createWritable()
      await writable.write(blob)
      await writable.close()
      return { mode: 'picker', cancelled: false, filename }
    } catch (error) {
      // AbortError == the user dismissed the dialog; anything else falls back.
      if (error instanceof DOMException && error.name === 'AbortError') {
        return { mode: 'picker', cancelled: true, filename }
      }
      // SecurityError / NotAllowedError (no user gesture, sandboxed iframe…)
      // -> fall through to the anchor download.
    }
  }

  triggerDownload(blob, filename)
  return { mode: 'download', cancelled: false, filename }
}

/** Classic `<a download>` fallback. */
export function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  // Revoke on the next tick so the download has started.
  setTimeout(() => URL.revokeObjectURL(url), 10_000)
}
