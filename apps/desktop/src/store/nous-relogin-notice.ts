import { dismissNotification } from '@/store/notifications'

/**
 * The single notice the keepalive's terminal-auth-death event raises. Its `kind: 'error'` toast
 * has no default duration, so it is sticky by design: it stays until something takes it down.
 * Losing the login is the reason it exists, so the sign-in that repairs the login retires it.
 */
export const NOUS_RELOGIN_NOTICE_ID = 'nous-auth-relogin-required'

/** The Nous login works again, so the notice the outage raised no longer describes anything. */
export function clearNousReloginNotice(): void {
  dismissNotification(NOUS_RELOGIN_NOTICE_ID)
}
