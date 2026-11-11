import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { handleAuthReloginEvent } from '@/app/session/hooks/use-message-stream/gateway-event/auth-relogin'
import type { GatewayEventContext } from '@/app/session/hooks/use-message-stream/gateway-event/types'
import type * as HermesApi from '@/hermes'
import type { FreeTierRequester } from '@/store/free-tier'
import { $notifications, dismissNotification } from '@/store/notifications'

import { $freeTierSignIn, beginFreeTierSignIn, closeFreeTierSignIn } from './free-tier-sign-in'

const startOAuthLogin = vi.fn()
const pollOAuthSession = vi.fn()
const getNousHandoffStatus = vi.fn()

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<typeof HermesApi>()),
  cancelOAuthSession: async () => ({ ok: true }),
  getNousHandoffStatus: (since: null | string) => getNousHandoffStatus(since),
  listOAuthProviders: async () => ({ providers: [] }),
  pollOAuthSession: (providerId: string, sessionId: string, _profile?: unknown, wait?: number) =>
    pollOAuthSession(providerId, sessionId, wait),
  startOAuthLogin: () => startOAuthLogin()
}))

const NOTICE_ID = 'nous-auth-relogin-required'

const requestGateway = (async <T>(_method: string, _params?: Record<string, unknown>): Promise<T> =>
  ({ available: true, has_guest: true }) as T) satisfies FreeTierRequester

const start = (id: string) => ({
  expires_in: 900,
  flow: 'device_code' as const,
  poll_interval: 2,
  session_id: id,
  user_code: 'ABCD-EFGH',
  verification_url: `https://portal.example/claim?code=${id}`
})

/** The gateway event the keepalive broadcasts when the stored Nous login died. */
function reloginContext(): GatewayEventContext {
  const payload = { message: 'No access token found for Nous Portal login.' }

  return {
    deps: {} as GatewayEventContext['deps'],
    event: { payload, type: 'nous_auth.relogin_required' },
    explicitSid: '',
    fromActiveSource: () => true,
    isActiveEvent: false,
    occurredAt: 1_700_000_100,
    payload,
    scheduleConfigRefresh: vi.fn(),
    sessionId: null
  } as GatewayEventContext
}

const noticeShowing = () => $notifications.get().some(item => item.id === NOTICE_ID)

beforeEach(() => {
  vi.useFakeTimers()
  vi.spyOn(window, 'open').mockReturnValue(null)
})

afterEach(() => {
  closeFreeTierSignIn()
  dismissNotification(NOTICE_ID)
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe('the Nous disconnect notice', () => {
  it('goes away once the user signs in again', async () => {
    expect(handleAuthReloginEvent(reloginContext())).toBe(true)
    expect(noticeShowing()).toBe(true)

    startOAuthLogin.mockResolvedValueOnce(start('session-relogin'))
    pollOAuthSession.mockResolvedValue({
      account_email: 'someone@example.com',
      model: null,
      session_id: 'session-relogin',
      status: 'approved'
    })

    await beginFreeTierSignIn(requestGateway)
    await vi.advanceTimersByTimeAsync(10)

    expect($freeTierSignIn.get().status).toBe('completed')
    expect(noticeShowing()).toBe(false)
  })

  it('stays up while the sign-in is still waiting for the user', async () => {
    expect(handleAuthReloginEvent(reloginContext())).toBe(true)

    startOAuthLogin.mockResolvedValueOnce(start('session-pending'))
    pollOAuthSession.mockResolvedValue({ session_id: 'session-pending', status: 'pending' })

    await beginFreeTierSignIn(requestGateway)
    await vi.advanceTimersByTimeAsync(10)

    expect($freeTierSignIn.get().status).toBe('code')
    expect(noticeShowing()).toBe(true)
  })
})
