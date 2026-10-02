import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { buildPrepaintScript, resolveThemeAttrs } from './prepaint';
import { createThemeStore } from './store';
import { setTestScope, stubColorScheme, TEST_CONFIG as CFG } from './testing';
import { MODES, THEME_COLOR, THEME_IDS } from './themes';

const scopeRaw = (id: string) => JSON.stringify({ state: { active: id }, version: 1 });
const kitRaw = (byScope: Record<string, { theme: string; mode: string }>, fallback = { theme: 'wave', mode: 'dark' }) =>
  JSON.stringify({ state: { byScope, fallback }, version: 4 });
const flatRaw = (theme: string, mode: string) => JSON.stringify({ state: { theme, mode }, version: 3 });

describe('resolveThemeAttrs', () => {
  it('uses the config fallback with nothing stored and no scope (the OS preference does not matter)', () => {
    expect(resolveThemeAttrs(null, null, false, CFG)).toEqual({ theme: 'wave', mode: 'dark', resolved: 'dark', scope: null });
    expect(resolveThemeAttrs(null, null, true, CFG)).toEqual({ theme: 'wave', mode: 'dark', resolved: 'dark', scope: null });
  });

  it("opens a scope nobody has picked for in that scope's seed", () => {
    expect(resolveThemeAttrs(null, scopeRaw('work'), false, CFG)).toEqual({ theme: 'law', mode: 'light', resolved: 'light', scope: 'work' });
    expect(resolveThemeAttrs(kitRaw({}), scopeRaw('personal'), false, CFG).theme).toBe('wave');
  });

  it("paints each scope's own saved choice", () => {
    const raw = kitRaw({ work: { theme: 'tokyo', mode: 'mid' }, personal: { theme: 'mint', mode: 'light' } });
    expect(resolveThemeAttrs(raw, scopeRaw('work'), false, CFG)).toMatchObject({ theme: 'tokyo', resolved: 'mid', scope: 'work' });
    expect(resolveThemeAttrs(raw, scopeRaw('personal'), true, CFG)).toMatchObject({ theme: 'mint', resolved: 'light', scope: 'personal' });
  });

  it('gives an unseeded scope with no saved choice the stored fallback', () => {
    expect(resolveThemeAttrs(kitRaw({}, { theme: 'cops', mode: 'light' }), scopeRaw('other'), false, CFG)).toMatchObject({ theme: 'cops', mode: 'light' });
  });

  it('reads a pre-kit flat {theme, mode} ahead of the seed, before the store has migrated it', () => {
    expect(resolveThemeAttrs(flatRaw('sunset', 'dark'), scopeRaw('work'), false, CFG)).toMatchObject({ theme: 'sunset', mode: 'dark' });
  });

  it("reads ADA's pre-kit flat {preset, mode} the same way, so its first load after the kit does not flash", () => {
    const raw = JSON.stringify({ state: { mode: 'light', preset: 'tokyo' }, version: 0 });
    expect(resolveThemeAttrs(raw, scopeRaw('work'), true, CFG)).toMatchObject({ theme: 'tokyo', mode: 'light', resolved: 'light' });
  });

  it('keeps mid as its own resolved mode; system resolves from the OS and never picks mid', () => {
    expect(resolveThemeAttrs(kitRaw({ work: { theme: 'wave', mode: 'mid' } }), scopeRaw('work'), true, CFG).resolved).toBe('mid');
    expect(resolveThemeAttrs(kitRaw({ work: { theme: 'mint', mode: 'system' } }), scopeRaw('work'), true, CFG).resolved).toBe('dark');
    expect(resolveThemeAttrs(kitRaw({ work: { theme: 'mint', mode: 'system' } }), scopeRaw('work'), false, CFG).resolved).toBe('light');
  });

  it('falls back field by field on unknown values, and on garbage in either entry', () => {
    expect(resolveThemeAttrs(kitRaw({ work: { theme: 'neon', mode: 'mid' } }), scopeRaw('work'), false, CFG)).toMatchObject({ theme: 'law', mode: 'mid' });
    expect(resolveThemeAttrs('{not json', scopeRaw('work'), true, CFG)).toMatchObject({ theme: 'law', mode: 'light' });
    expect(resolveThemeAttrs(kitRaw({}), '{not json', true, CFG)).toMatchObject({ theme: 'wave', scope: null });
    expect(resolveThemeAttrs('null', 'null', true, CFG).theme).toBe('wave');
  });

  it('ignores the scope entry when the app has no scope source', () => {
    const raw = kitRaw({ work: { theme: 'tokyo', mode: 'mid' } }, { theme: 'law', mode: 'light' });
    expect(resolveThemeAttrs(raw, scopeRaw('work'), false, { ...CFG, scope: null })).toMatchObject({ theme: 'law', scope: null });
  });

  it('keeps its inlined id lists equal to the catalogue (it must stay self-contained)', () => {
    const src = resolveThemeAttrs.toString();
    const lists = new Map<string, string[]>();
    for (const m of src.matchAll(/(themes|modes)\s*=\s*\[([^\]]*)\]/g)) {
      lists.set(m[1], [...m[2].matchAll(/["']([a-z]+)["']/g)].map((x) => x[1]));
    }
    expect(lists.get('themes') ?? []).toEqual([...THEME_IDS]);
    expect(lists.get('modes') ?? []).toEqual([...MODES]);
    for (const id of THEME_IDS) expect(resolveThemeAttrs(kitRaw({ work: { theme: id, mode: 'light' } }), scopeRaw('work'), false, CFG).theme).toBe(id);
  });
});

describe('pre-paint script (the exact string inlined into index.html)', () => {
  let scheme: ReturnType<typeof stubColorScheme>;
  beforeEach(() => {
    scheme = stubColorScheme(false);
    document.documentElement.removeAttribute('data-theme');
    document.documentElement.removeAttribute('data-mode');
    document.head.innerHTML = '<meta name="theme-color" content="">';
  });
  afterEach(() => {
    scheme.restore();
    window.localStorage.clear();
  });

  const run = () => new Function(buildPrepaintScript(CFG))();
  const html = () => document.documentElement;
  const meta = () => document.querySelector('meta[name="theme-color"]')?.getAttribute('content');

  it('paints the fallback before any React code runs', () => {
    run();
    expect(html().dataset.theme).toBe('wave');
    expect(html().dataset.mode).toBe('dark');
    expect(html().style.colorScheme).toBe('dark');
    expect(meta()).toBe(THEME_COLOR.wave.dark);
  });

  it("paints the current scope's theme: switching scope and reloading switches the look", () => {
    window.localStorage.setItem(CFG.storageKey, kitRaw({ personal: { theme: 'law', mode: 'mid' } }));
    setTestScope('work');
    run();
    expect([html().dataset.theme, html().dataset.mode]).toEqual(['law', 'light']);
    setTestScope('personal');
    run();
    expect([html().dataset.theme, html().dataset.mode, html().style.colorScheme]).toEqual(['law', 'mid', 'dark']);
    expect(meta()).toBe(THEME_COLOR.law.mid);
  });

  it('agrees with the store: what a scope saves is what the next load paints for it', () => {
    const useStore = createThemeStore(CFG);
    useStore.getState().setTheme('work', 'sunset');
    useStore.getState().setMode('work', 'dark');
    setTestScope('work');
    run();
    expect([html().dataset.theme, html().dataset.mode]).toEqual(['sunset', 'dark']);
    expect(meta()).toBe(THEME_COLOR.sunset.dark);
    setTestScope('personal');
    run();
    expect(html().dataset.theme).toBe('wave');
  });

  it('survives storage that throws', () => {
    const orig = Object.getOwnPropertyDescriptor(window, 'localStorage');
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      get() {
        throw new Error('blocked');
      },
    });
    try {
      expect(run).not.toThrow();
      expect(html().dataset.theme).toBe('wave');
    } finally {
      if (orig) Object.defineProperty(window, 'localStorage', orig);
    }
  });
});
