/**
 * Pre-paint theme resolution.
 *
 * `resolveThemeAttrs` is the ONE implementation of "stored preference + current scope + OS preference
 * -> <html data-theme / data-mode>". The React side uses the same store shape at runtime, and the app's
 * vite.config.ts inlines this function's compiled source (Function.prototype.toString) into index.html
 * as a blocking script through `buildPrepaintScript`, so the first paint already has the right theme,
 * for the right account or user. Painting a default and then flipping (ADA before the kit) is a flash.
 *
 * Because its source is inlined, the function must stay self-contained: no imports, no closures over
 * module constants, no helpers. Its config arrives as an argument, serialised by the app. The id lists
 * are repeated inside it on purpose, and prepaint.test.ts asserts they equal THEME_IDS / MODES.
 *
 * Imported by vite.config.ts, so: no app alias and no DOM access at module level.
 */
import type { PrepaintConfig, ThemeKitConfig } from './config';
import { prepaintConfig } from './config';
import { THEME_COLOR } from './themes';

export interface ThemeAttrs {
  theme: 'cops' | 'law' | 'aurora' | 'sunset' | 'mint' | 'tokyo' | 'wave';
  mode: 'system' | 'light' | 'mid' | 'dark';
  resolved: 'light' | 'mid' | 'dark';
  /** The scope key the theme was resolved for, or null with no scope. */
  scope: string | null;
}

/**
 * @param raw the localStorage value under `cfg.storageKey` (the kit store's persist JSON), or null
 * @param rawScope the localStorage value under `cfg.scope.storageKey`, or null
 * @param prefersDark whether `(prefers-color-scheme: dark)` currently matches
 * @param cfg the app's PrepaintConfig
 *
 * Order: the scope's saved choice, then (before the store has migrated) a flat pre-kit {theme, mode}
 * (ADA's pre-kit store named the theme `preset`: {preset, mode}),
 * then the scope's seed, then the store's own fallback, then the config's fallback. Each field falls
 * back on its own, so a bad theme keeps a good mode.
 */
export function resolveThemeAttrs(raw: string | null, rawScope: string | null, prefersDark: boolean, cfg: PrepaintConfig): ThemeAttrs {
  const themes = ['cops', 'law', 'aurora', 'sunset', 'mint', 'tokyo', 'wave'];
  const modes = ['system', 'light', 'mid', 'dark'];
  const obj = (v: unknown): Record<string, unknown> | null => (v && typeof v === 'object' ? (v as Record<string, unknown>) : null);
  let scope: string | null = null;
  try {
    let node: unknown = rawScope ? JSON.parse(rawScope) : null;
    const path = cfg.scope ? cfg.scope.path : [];
    for (let i = 0; i < path.length && node !== null && node !== undefined; i++) node = obj(node) ? (obj(node) as Record<string, unknown>)[path[i] as string] : null;
    if (cfg.scope && typeof node === 'string' && node) scope = node;
  } catch {
    scope = null;
  }
  let state: Record<string, unknown> | null = null;
  try {
    const parsed = obj(raw ? JSON.parse(raw) : null);
    state = parsed && obj(parsed.state) ? obj(parsed.state) : parsed;
  } catch {
    // Unreadable preference: `state` stays null, so the paint falls through to the seed and the fallback.
  }
  const byScope = state ? obj(state.byScope) : null;
  const picks: Array<Record<string, unknown> | null> = [];
  if (scope && byScope) picks.push(obj(byScope[scope]));
  if (state && !byScope) picks.push(state.theme === undefined && state.preset !== undefined ? { theme: state.preset, mode: state.mode } : state);
  if (scope) picks.push(obj(cfg.seeds[scope]));
  if (state) picks.push(obj(state.fallback));
  picks.push(obj(cfg.fallback));
  let theme: string | null = null;
  let mode: string | null = null;
  for (let i = 0; i < picks.length; i++) {
    const p = picks[i];
    if (!p) continue;
    if (theme === null && themes.indexOf(p.theme as string) !== -1) theme = p.theme as string;
    if (mode === null && modes.indexOf(p.mode as string) !== -1) mode = p.mode as string;
  }
  theme = theme || 'wave';
  mode = mode || 'dark';
  const resolved = mode === 'system' ? (prefersDark ? 'dark' : 'light') : mode;
  return { theme, mode, resolved, scope } as ThemeAttrs;
}

/** The inline <script> body index.html runs before first paint, for an app's ThemeKitConfig. */
export function buildPrepaintScript(config: ThemeKitConfig): string {
  const cfg = prepaintConfig(config);
  const scopeKey = cfg.scope ? cfg.scope.storageKey : null;
  return [
    '(function(){try{',
    'var ls=null;try{ls=window.localStorage;}catch(e){}',
    'var get=function(k){try{return ls&&k?ls.getItem(k):null;}catch(e){return null;}};',
    'var dark=!!(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches);',
    'var a=(' + resolveThemeAttrs.toString() + ')(get(' + JSON.stringify(cfg.storageKey) + '),get(' + JSON.stringify(scopeKey) + '),dark,' + JSON.stringify(cfg) + ');',
    'var h=document.documentElement;',
    'h.setAttribute("data-theme",a.theme);h.setAttribute("data-mode",a.resolved);h.style.colorScheme=a.resolved==="light"?"light":"dark";',
    'var c=' + JSON.stringify(THEME_COLOR) + ';',
    'var m=document.querySelector("meta[name=theme-color]");if(m&&c[a.theme])m.setAttribute("content",c[a.theme][a.resolved]);',
    '}catch(e){}})();',
  ].join('');
}
