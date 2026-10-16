/**
 * Minimal DOM globals for component specs.
 *
 * jsdom implements neither `ResizeObserver` nor `matchMedia`, and Element Plus
 * touches both (popper positioning, responsive table/select internals). Without
 * these, mounting any component that renders an `el-tooltip` or `el-select`
 * throws during setup, which looks like a component bug rather than a missing
 * browser API.
 *
 * Kept here rather than in a global vitest setup file so the pure-logic specs
 * keep running in the fast `node` environment and never pay for jsdom.
 */

export function installDomGlobals(): void {
  if (typeof window === 'undefined') return

  const globalWindow = window as unknown as Record<string, unknown>

  if (typeof globalWindow.ResizeObserver === 'undefined') {
    class NoopResizeObserver {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    }
    globalWindow.ResizeObserver = NoopResizeObserver
  }

  if (typeof window.matchMedia !== 'function') {
    globalWindow.matchMedia = (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    })
  }

  // jsdom defines these as throwing "not implemented".
  if (typeof window.scrollTo !== 'function') {
    globalWindow.scrollTo = () => {}
  }
  const elementProto = window.Element?.prototype as unknown as
    | Record<string, unknown>
    | undefined
  if (elementProto && typeof elementProto.scrollIntoView !== 'function') {
    elementProto.scrollIntoView = () => {}
  }
}
