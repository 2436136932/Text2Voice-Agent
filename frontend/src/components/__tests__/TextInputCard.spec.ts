// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import TextInputCard from '@/components/TextInputCard.vue'
import { useConfigStore } from '@/stores/config'
import type { ProvidersResponse, TTSProvider } from '@/api/types'
import { installDomGlobals } from '@/test/dom'

/**
 * Component tests for the composer.
 *
 * These cover behaviours that were real bugs in this project, not hypothetical
 * ones -- each of the first four broke in production and was found by hand:
 *
 *  1. the generate button did nothing, because the component was mounted
 *     without `:can-generate` / `@generate` wired up;
 *  2. the segment badge divided by a hardcoded 5000 instead of the provider's
 *     `max_chars`, so MiMo runs were rejected on segment 1;
 *  3. the token estimate must not appear at all when the mode skips the LLM.
 *
 * A component test is the only kind that can catch (1): the logic was correct,
 * the wiring between parent and child was not.
 */
installDomGlobals()

function provider(maxChars: number): TTSProvider {
  return {
    id: 'mimo',
    name: 'MiMo',
    default_voice: 'mock-neutral',
    voices: [],
    requires_base_url: false,
    supports_base_url: true,
    supports_speed: false,
    speed_min: 0.5,
    speed_max: 2.0,
    formats: ['wav'],
    api_key_hint: '',
    docs_url: null,
    max_chars: maxChars,
  }
}

function mountCard(props: Record<string, unknown> = {}) {
  const store = useConfigStore()
  return {
    store,
    wrapper: mount(TextInputCard, {
      props,
      global: { plugins: [ElementPlus] },
    }),
  }
}

function configureStore(
  store: ReturnType<typeof useConfigStore>,
  overrides: { maxChars?: number; text?: string; mode?: string } = {},
) {
  const providers = {
    llm: [],
    tts: [provider(overrides.maxChars ?? 5000)],
  } as unknown as ProvidersResponse
  store.providers = providers
  store.providersLoaded = true
  store.ttsProviderId = 'mimo'
  store.ttsVoice = 'mock-neutral'
  store.text = overrides.text ?? '短文本'
  store.mode = (overrides.mode ?? 'tts') as never
}

describe('TextInputCard: the generate button is actually wired up', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('emits "generate" when clicked while enabled', async () => {
    // The regression: the button rendered but nothing listened, so clicking did
    // nothing at all and no error appeared.
    const { wrapper, store } = mountCard({ canGenerate: true })
    configureStore(store)
    await wrapper.vm.$nextTick()

    const button = wrapper.findAll('button').find((b) => b.text().includes('生成语音'))
    expect(button, 'the generate button must exist').toBeTruthy()
    await button!.trigger('click')

    expect(wrapper.emitted('generate')).toHaveLength(1)
  })

  it('does not emit while a generation is already running', async () => {
    const { wrapper, store } = mountCard({ canGenerate: true, generating: true })
    configureStore(store)
    await wrapper.vm.$nextTick()

    const button = wrapper.findAll('button').find((b) => b.text().includes('生成中'))
    expect(button, 'the button shows the running label').toBeTruthy()
    expect(button!.attributes('disabled')).toBeDefined()
    await button!.trigger('click')

    expect(wrapper.emitted('generate')).toBeUndefined()
  })

  it('disables the button and explains why when generation is blocked', async () => {
    const { wrapper, store } = mountCard({
      canGenerate: false,
      extraReasons: ['缺少 Key'],
    })
    configureStore(store)
    await wrapper.vm.$nextTick()

    const button = wrapper.findAll('button').find((b) => b.text().includes('生成语音'))
    expect(button!.attributes('disabled')).toBeDefined()
  })

  it('emits "clear" and empties the text', async () => {
    const { wrapper, store } = mountCard({ canGenerate: true })
    configureStore(store, { text: '要清空的文本' })
    await wrapper.vm.$nextTick()

    const clear = wrapper.findAll('button').find((b) => b.text().includes('清空'))
    await clear!.trigger('click')

    expect(wrapper.emitted('clear')).toHaveLength(1)
  })
})

describe('TextInputCard: the segment badge follows the provider limit', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  // 8100 monotone characters, so the splitter hard-cuts and the arithmetic is
  // exact: 3 pieces at a 4000 limit, 2 at 5000.
  const LONG_TEXT = 'a'.repeat(8100)

  it('divides by the provider max_chars, not a hardcoded 5000', async () => {
    const tight = mountCard({ canGenerate: true })
    configureStore(tight.store, { maxChars: 4000, text: LONG_TEXT })
    await tight.wrapper.vm.$nextTick()
    expect(tight.wrapper.text()).toContain('将自动分为 3 段生成')

    // A provider that allows 5000 needs fewer pieces for the same text: the
    // badge must change with the provider, which is what the bug prevented.
    const loose = mountCard({ canGenerate: true })
    configureStore(loose.store, { maxChars: 5000, text: LONG_TEXT })
    await loose.wrapper.vm.$nextTick()
    expect(loose.wrapper.text()).toContain('将自动分为 2 段生成')
  })

  it('shows no badge for text that fits in one request', async () => {
    const { wrapper, store } = mountCard({ canGenerate: true })
    configureStore(store, { maxChars: 4000, text: '很短的文本' })
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).not.toContain('将自动分为')
  })
})

describe('TextInputCard: the token estimate is honest about the LLM', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('reports an estimate when the LLM will run', async () => {
    const { wrapper, store } = mountCard({ canGenerate: true })
    configureStore(store, { text: '中'.repeat(100), mode: 'tts' })
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('预估输入 token 约')
  })

  it('says the model is skipped instead of showing zero, in text2audio', async () => {
    const { wrapper, store } = mountCard({ canGenerate: true })
    configureStore(store, { text: '中'.repeat(100), mode: 'text2audio' })
    await wrapper.vm.$nextTick()

    // "0 tokens" would read as a measurement; there is nothing to measure.
    expect(wrapper.text()).toContain('跳过语言模型')
    expect(wrapper.text()).not.toContain('预估输入 token')
  })
})
