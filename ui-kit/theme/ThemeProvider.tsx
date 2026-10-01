/**
 * ThemeProvider: the current scope's {theme, mode} from the kit store, the stored mode resolved into
 * light/mid/dark (system picks only light or dark, following the OS live), mirrored onto <html>
 * (data-theme, data-mode, color-scheme, theme-color meta). Components read it with `useTheme()`.
 *
 * The app passes its store, its config and the CURRENT scope key (the console's active account, ADA's
 * active user). A new scope is a new theme: switching account or user restyles the app inside the same
 * view-transition crossfade as picking a theme.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useSyncExternalStore, type ReactNode } from 'react';
import { applyThemeToDocument, systemPrefersDark } from './apply';
import type { ThemeKitConfig, ThemePrefs } from './config';
import { prefsFor as storePrefsFor, type ThemeStore } from './store';
import { resolveMode, type ResolvedMode, type ThemeId, type ThemeMode } from './themes';

export interface ThemeContextValue {
  theme: ThemeId;
  mode: ThemeMode;
  resolved: ResolvedMode;
  /** The scope this theme belongs to, or null with no scope. */
  scope: string | null;
  setTheme: (theme: ThemeId) => void;
  setMode: (mode: ThemeMode) => void;
  /** Another scope's theme (a switch list shows each account's own color), and its resolved mode. */
  prefsFor: (scope: string | null) => ThemePrefs & { resolved: ResolvedMode };
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

function subscribeSystem(onChange: () => void): () => void {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {};
  const mq = window.matchMedia('(prefers-color-scheme: dark)');
  mq.addEventListener('change', onChange);
  return () => mq.removeEventListener('change', onChange);
}

export interface ThemeProviderProps {
  store: ThemeStore;
  config: ThemeKitConfig;
  scope: string | null;
  children: ReactNode;
}

export function ThemeProvider({ store, config, scope, children }: ThemeProviderProps) {
  const byScope = store((s) => s.byScope);
  const fallback = store((s) => s.fallback);
  const storeSetTheme = store((s) => s.setTheme);
  const storeSetMode = store((s) => s.setMode);
  const systemDark = useSyncExternalStore(subscribeSystem, systemPrefersDark, () => false);
  const { theme, mode } = storePrefsFor({ byScope, fallback }, scope, config);
  const resolved = resolveMode(mode, systemDark);

  // A change after the first paint crossfades the whole app (motion.css `::view-transition-*(root)`) where
  // the browser has view transitions and motion is not reduced; otherwise it swaps at once.
  const painted = useRef(false);
  useEffect(() => {
    const apply = () => applyThemeToDocument(theme, resolved);
    const reduced = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (painted.current && !reduced && typeof document.startViewTransition === 'function') {
      const root = document.documentElement;
      root.dataset.vt = 'theme';
      document.startViewTransition(apply).finished.finally(() => delete root.dataset.vt);
    } else apply();
    painted.current = true;
  }, [theme, resolved]);

  const setTheme = useCallback((t: ThemeId) => storeSetTheme(scope, t), [storeSetTheme, scope]);
  const setMode = useCallback((m: ThemeMode) => storeSetMode(scope, m), [storeSetMode, scope]);
  const prefsFor = useCallback(
    (s: string | null) => {
      const p = storePrefsFor({ byScope, fallback }, s, config);
      return { ...p, resolved: resolveMode(p.mode, systemDark) };
    },
    [byScope, fallback, config, systemDark],
  );

  const value = useMemo(
    () => ({ theme, mode, resolved, scope, setTheme, setMode, prefsFor }),
    [theme, mode, resolved, scope, setTheme, setMode, prefsFor],
  );
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components -- the hook belongs with its provider
export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error('useTheme must be used within a ThemeProvider');
  return ctx;
}
