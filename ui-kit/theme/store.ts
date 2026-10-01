/**
 * Theme store: one {theme, mode} per scope key, persisted to localStorage under the app's storageKey.
 *
 *   {state: {byScope: {work: {theme: 'law', mode: 'system'}, personal: {...}}, fallback: {...}}, version: 4}
 *
 * A scope is whatever the app says it is (config.ts): the console's account, ADA's user. A scope with
 * no saved choice gets its seed, then the fallback (`prefsFor`). The persisted JSON is read back by the
 * pre-paint script (prepaint.ts), so its shape is part of that contract.
 *
 * Version 4 is the kit's first. Anything older is the app's own pre-kit shape: the app's `legacy`
 * turns it into one {theme, mode}, which becomes the CURRENT scope's entry (read from the app's scope
 * source at that moment), so a choice made before themes were per scope stays on the scope it was
 * made in. With no scope yet, it becomes the fallback.
 */
import { create } from 'zustand';
import { createJSONStorage, persist } from 'zustand/middleware';
import { KIT_STORE_VERSION, type ThemeKitConfig, type ThemePrefs } from './config';
import { safeLocalStorage } from './storage';
import { MODES, THEME_IDS, type ThemeId, type ThemeMode } from './themes';

export interface ThemeStoreState {
  byScope: Record<string, ThemePrefs>;
  fallback: ThemePrefs;
  /** Save a theme for a scope (null: the fallback). The other field keeps what that scope shows now. */
  setTheme: (scope: string | null, theme: ThemeId) => void;
  setMode: (scope: string | null, mode: ThemeMode) => void;
}

function valid(p: unknown): ThemePrefs | null {
  if (!p || typeof p !== 'object') return null;
  const { theme, mode } = p as Partial<ThemePrefs>;
  return (THEME_IDS as readonly string[]).includes(theme as string) && (MODES as readonly string[]).includes(mode as string)
    ? { theme: theme as ThemeId, mode: mode as ThemeMode }
    : null;
}

/** What a scope shows: its saved choice, else its seed, else the fallback. */
export function prefsFor(state: Pick<ThemeStoreState, 'byScope' | 'fallback'>, scope: string | null, config: ThemeKitConfig): ThemePrefs {
  if (scope) return state.byScope[scope] ?? config.seeds[scope] ?? state.fallback;
  return state.fallback;
}

/** The app's current scope key, read from its storage the same way the pre-paint script reads it. */
export function readScope(config: ThemeKitConfig): string | null {
  if (!config.scope) return null;
  try {
    let node: unknown = JSON.parse(safeLocalStorage.getItem(config.scope.storageKey) as string);
    for (const key of config.scope.path) node = node && typeof node === 'object' ? (node as Record<string, unknown>)[key] : null;
    return typeof node === 'string' && node ? node : null;
  } catch {
    return null;
  }
}

export type ThemeStore = ReturnType<typeof createThemeStore>;

/** One store per app, created once at module level from the app's config. */
export function createThemeStore(config: ThemeKitConfig) {
  return create<ThemeStoreState>()(
    persist(
      (set) => {
        const write = (scope: string | null, patch: Partial<ThemePrefs>) =>
          set((s) => {
            const next = { ...prefsFor(s, scope, config), ...patch };
            return scope ? { byScope: { ...s.byScope, [scope]: next } } : { fallback: next };
          });
        return {
          byScope: {},
          fallback: config.fallback,
          setTheme: (scope, theme) => write(scope, { theme }),
          setMode: (scope, mode) => write(scope, { mode }),
        };
      },
      {
        name: config.storageKey,
        version: KIT_STORE_VERSION,
        storage: createJSONStorage(() => safeLocalStorage),
        partialize: (s) => ({ byScope: s.byScope, fallback: s.fallback }),
        migrate: (persisted, version) => {
          if (version >= KIT_STORE_VERSION) return persisted as Pick<ThemeStoreState, 'byScope' | 'fallback'>;
          const kept = valid(config.legacy ? config.legacy(persisted, version) : persisted);
          const scope = readScope(config);
          if (!kept) return { byScope: {}, fallback: config.fallback };
          return scope ? { byScope: { [scope]: kept }, fallback: config.fallback } : { byScope: {}, fallback: kept };
        },
        // A hand-edited or half-written entry never reaches the page: each saved scope is checked.
        merge: (persisted, current) => {
          const p = (persisted ?? {}) as Partial<Pick<ThemeStoreState, 'byScope' | 'fallback'>>;
          const byScope: Record<string, ThemePrefs> = {};
          for (const [k, v] of Object.entries(p.byScope ?? {})) {
            const ok = valid(v);
            if (ok) byScope[k] = ok;
          }
          return { ...current, byScope, fallback: valid(p.fallback) ?? current.fallback };
        },
      },
    ),
  );
}
