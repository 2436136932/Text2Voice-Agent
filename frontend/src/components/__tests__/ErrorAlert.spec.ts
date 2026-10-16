// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import ErrorAlert from '@/components/ErrorAlert.vue'
import { ApiError } from '@/api/http'
import { installDomGlobals } from '@/test/dom'

/**
 * Component tests for the error panel.
 *
 * The behaviour worth pinning is the `detail.suggestion` line. The backend puts
 * the *actionable* instruction there -- for an interrupted task, "密钥不会落盘，
 * 因此无法自动续跑。请返回生成页重新提交任务。" -- and it used to be reachable only
 * inside the collapsed JSON block, so the one sentence telling the user what to
 * do next was hidden behind a disclosure triangle.
 */
installDomGlobals()

function mountAlert(error: ApiError | null, props: Record<string, unknown> = {}) {
  return mount(ErrorAlert, {
    props: { error, ...props },
    global: { plugins: [ElementPlus] },
  })
}

describe('ErrorAlert', () => {
  it('renders nothing when there is no error', () => {
    const wrapper = mountAlert(null)
    expect(wrapper.find('.error-alert').exists()).toBe(false)
  })

  it('always shows the human-readable message', () => {
    const wrapper = mountAlert(
      new ApiError({ code: 'TTS_AUTH_FAILED', message: '语音合成密钥无效' }),
    )
    expect(wrapper.find('.error-alert__message').text()).toBe('语音合成密钥无效')
  })

  it('surfaces detail.suggestion as visible text, not just collapsed JSON', () => {
    const suggestion = '任务在服务重启时被中断，请返回生成页重新提交任务。'
    const wrapper = mountAlert(
      new ApiError({
        code: 'TASK_INTERRUPTED',
        message: '服务已重启，该任务已中断。',
        detail: { suggestion, retryable: true },
      }),
    )

    const line = wrapper.find('.error-alert__suggestion')
    expect(line.exists(), 'the suggestion must have its own visible element').toBe(true)
    expect(line.text()).toContain(suggestion)
    expect(line.text()).toContain('建议')
  })

  it('omits the suggestion line when the backend sent none', () => {
    const wrapper = mountAlert(
      new ApiError({ code: 'LLM_TIMEOUT', message: '超时', detail: { retryable: true } }),
    )
    expect(wrapper.find('.error-alert__suggestion').exists()).toBe(false)
  })

  it('ignores a non-string suggestion rather than rendering junk', () => {
    const wrapper = mountAlert(
      new ApiError({
        code: 'LLM_ERROR',
        message: '失败',
        detail: { suggestion: { nested: true } },
      }),
    )
    expect(wrapper.find('.error-alert__suggestion').exists()).toBe(false)
  })

  it('shows the error code so a report can name it', () => {
    const wrapper = mountAlert(
      new ApiError({ code: 'TTS_TEXT_TOO_LONG', message: '文本过长' }),
    )
    expect(wrapper.text()).toContain('TTS_TEXT_TOO_LONG')
  })

  it('emits "retry" when the retry button is used', async () => {
    const wrapper = mountAlert(
      new ApiError({ code: 'LLM_ERROR', message: '失败' }),
      { retryable: true },
    )

    const retry = wrapper.findAll('button').find((b) => b.text().includes('重试'))
    expect(retry, 'a retryable error must offer a retry button').toBeTruthy()
    await retry!.trigger('click')

    expect(wrapper.emitted('retry')).toHaveLength(1)
  })

  it('hides the retry button when the error is not retryable', () => {
    const wrapper = mountAlert(new ApiError({ code: 'TTS_AUTH_FAILED', message: '密钥无效' }), {
      retryable: false,
    })
    const retry = wrapper.findAll('button').find((b) => b.text().includes('重试'))
    expect(retry).toBeUndefined()
  })
})
