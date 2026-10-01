/**
 * Test helpers for the kit's own tests, so they run unchanged in any app's vitest (jsdom) without that
 * app's setup file: a config with two scopes, and a controllable prefers-color-scheme.
 */
import type { ThemeKitConfig } from './config';

export const TEST_CONFIG: ThemeKitConfig = {
  storageKey: 'kit-test-theme',
  scope: { storageKey: 'kit-test-scope', path: ['state', 'active'] },
  fallback: { theme: 'wave', mode: 'dark' },
  seeds: { work: { theme: 'law', mode: 'light' }, personal: { theme: 'wave', mode: 'dark' } },
};

/** Save the current scope the way an app's Zustand persist entry would. */
export function setTestScope(id: string | null): void {
  if (id === null) window.localStorage.removeItem('kit-test-scope');
  else window.localStorage.setItem('kit-test-scope', JSON.stringify({ state: { active: id }, version: 1 }));
}

type Listener = (e: MediaQueryListEvent) => void;

/** Replace matchMedia with one whose dark preference the test controls; returns a setter and a restore. */
export function stubColorScheme(initialDark = false): { setDark: (dark: boolean) => void; restore: () => void } {
  const original = window.matchMedia;
  const listeners = new Set<Listener>();
  let dark = initialDark;
  window.matchMedia = ((query: string) => ({
    get matches() {
      return query.includes('prefers-color-scheme: dark') ? dark : false;
    },
    media: query,
    onchange: null,
    addEventListener: (_: string, l: Listener) => listeners.add(l),
    removeEventListener: (_: string, l: Listener) => listeners.delete(l),
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
  return {
    setDark(next) {
      dark = next;
      for (const l of listeners) l({ matches: next } as MediaQueryListEvent);
    },
    restore() {
      window.matchMedia = original;
    },
  };
}
