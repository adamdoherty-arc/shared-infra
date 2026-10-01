import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import type { ThemeKitConfig, ThemePrefs } from './config';
import { safeLocalStorage } from './storage';
import { createThemeStore, prefsFor, readScope } from './store';
import { ThemeProvider, useTheme } from './ThemeProvider';
import { setTestScope, stubColorScheme, TEST_CONFIG as CFG } from './testing';

let scheme: ReturnType<typeof stubColorScheme>;
beforeEach(() => {
  scheme = stubColorScheme(false);
});
afterEach(() => {
  scheme.restore();
  window.localStorage.clear();
});

const saved = () => JSON.parse(window.localStorage.getItem(CFG.storageKey) ?? '{}');

describe('theme store', () => {
  it('gives each scope its seed, and an unknown scope or no scope the fallback', () => {
    const s = createThemeStore(CFG).getState();
    expect(prefsFor(s, 'work', CFG)).toEqual({ theme: 'law', mode: 'light' });
    expect(prefsFor(s, 'personal', CFG)).toEqual({ theme: 'wave', mode: 'dark' });
    expect(prefsFor(s, 'other', CFG)).toEqual(CFG.fallback);
    expect(prefsFor(s, null, CFG)).toEqual(CFG.fallback);
  });

  it('saves a choice to its own scope only, keeping the other field from what the scope showed', () => {
    const useStore = createThemeStore(CFG);
    act(() => useStore.getState().setMode('work', 'dark'));
    expect(prefsFor(useStore.getState(), 'work', CFG)).toEqual({ theme: 'law', mode: 'dark' });
    expect(prefsFor(useStore.getState(), 'personal', CFG)).toEqual({ theme: 'wave', mode: 'dark' });
    expect(saved()).toEqual({ state: { byScope: { work: { theme: 'law', mode: 'dark' } }, fallback: CFG.fallback }, version: 4 });
    act(() => useStore.getState().setTheme(null, 'mint'));
    expect(saved().state.fallback).toEqual({ theme: 'mint', mode: 'dark' });
  });

  it('reads the scope key through the configured path, and nothing without a source', () => {
    setTestScope('personal');
    expect(readScope(CFG)).toBe('personal');
    expect(readScope({ ...CFG, scope: null })).toBeNull();
    window.localStorage.setItem('kit-test-scope', '{broken');
    expect(readScope(CFG)).toBeNull();
  });

  it('storage wrapper swallows a throwing localStorage', () => {
    const orig = Object.getOwnPropertyDescriptor(window, 'localStorage');
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      get() {
        throw new Error('blocked');
      },
    });
    try {
      expect(safeLocalStorage.getItem('x')).toBeNull();
      expect(() => safeLocalStorage.setItem('x', '1')).not.toThrow();
      expect(() => safeLocalStorage.removeItem('x')).not.toThrow();
    } finally {
      if (orig) Object.defineProperty(window, 'localStorage', orig);
    }
  });
});

describe("theme store migration from an app's pre-kit shape", () => {
  const legacyCfg: ThemeKitConfig = {
    ...CFG,
    legacy: (persisted, version) => {
      const p = persisted as { theme?: string; mode?: string } | null;
      if (version < 2 && p?.theme === 'aurora' && p.mode === 'system') return null;
      return p?.theme && p.mode ? ({ theme: p.theme === 'cops' ? 'wave' : p.theme, mode: p.mode } as ThemePrefs) : null;
    },
  };

  async function hydrateWith(state: unknown, version: number) {
    window.localStorage.setItem(CFG.storageKey, JSON.stringify({ state, version }));
    const useStore = createThemeStore(legacyCfg);
    await useStore.persist.rehydrate();
    return useStore.getState();
  }

  it("makes the saved {theme, mode} the current scope's entry", async () => {
    setTestScope('work');
    const s = await hydrateWith({ theme: 'tokyo', mode: 'mid' }, 3);
    expect(s.byScope).toEqual({ work: { theme: 'tokyo', mode: 'mid' } });
    expect(prefsFor(s, 'personal', legacyCfg)).toEqual({ theme: 'wave', mode: 'dark' });
  });

  it("runs the app's own legacy rules first (a saved COPS becomes Wave)", async () => {
    setTestScope('personal');
    expect((await hydrateWith({ theme: 'cops', mode: 'light' }, 2)).byScope).toEqual({ personal: { theme: 'wave', mode: 'light' } });
  });

  it('drops what the legacy rules drop, so every scope opens in its seed', async () => {
    setTestScope('work');
    const s = await hydrateWith({ theme: 'aurora', mode: 'system' }, 1);
    expect(s.byScope).toEqual({});
    expect(prefsFor(s, 'work', legacyCfg)).toEqual({ theme: 'law', mode: 'light' });
  });

  it('with no scope yet, keeps the saved choice as the fallback', async () => {
    setTestScope(null);
    const s = await hydrateWith({ theme: 'law', mode: 'dark' }, 3);
    expect(s.fallback).toEqual({ theme: 'law', mode: 'dark' });
  });

  it('drops a hand-edited entry that is not a theme', async () => {
    const s = await hydrateWith({ byScope: { work: { theme: 'neon', mode: 'dark' }, personal: { theme: 'mint', mode: 'mid' } }, fallback: { theme: 'law' } }, 4);
    expect(s.byScope).toEqual({ personal: { theme: 'mint', mode: 'mid' } });
    expect(s.fallback).toEqual(CFG.fallback);
  });
});

function Probe() {
  const { theme, resolved, scope, setTheme, prefsFor: other } = useTheme();
  return (
    <>
      <span data-testid="now">{`${scope}:${theme}:${resolved}`}</span>
      <span data-testid="other">{other('personal').theme}</span>
      <button type="button" onClick={() => setTheme('tokyo')}>
        tokyo
      </button>
    </>
  );
}

describe('ThemeProvider', () => {
  it("applies the current scope's theme to <html> and follows a scope switch", () => {
    const useStore = createThemeStore(CFG);
    const { rerender } = render(
      <ThemeProvider store={useStore} config={CFG} scope="work">
        <Probe />
      </ThemeProvider>,
    );
    const html = document.documentElement;
    expect(screen.getByTestId('now')).toHaveTextContent('work:law:light');
    expect([html.dataset.theme, html.dataset.mode, html.style.colorScheme]).toEqual(['law', 'light', 'light']);
    expect(screen.getByTestId('other')).toHaveTextContent('wave');

    act(() => screen.getByRole('button', { name: 'tokyo' }).click());
    expect(html.dataset.theme).toBe('tokyo');
    expect(prefsFor(useStore.getState(), 'personal', CFG).theme).toBe('wave');

    rerender(
      <ThemeProvider store={useStore} config={CFG} scope="personal">
        <Probe />
      </ThemeProvider>,
    );
    expect([html.dataset.theme, html.dataset.mode]).toEqual(['wave', 'dark']);
    expect(document.querySelector('meta[name="theme-color"]')?.getAttribute('content')).toBeTruthy();
  });

  it('follows the OS live while the mode is system', () => {
    const useStore = createThemeStore(CFG);
    act(() => useStore.getState().setMode('work', 'system'));
    render(
      <ThemeProvider store={useStore} config={CFG} scope="work">
        <Probe />
      </ThemeProvider>,
    );
    expect(screen.getByTestId('now')).toHaveTextContent('work:law:light');
    act(() => scheme.setDark(true));
    expect(screen.getByTestId('now')).toHaveTextContent('work:law:dark');
    expect(document.documentElement.dataset.mode).toBe('dark');
  });
});
