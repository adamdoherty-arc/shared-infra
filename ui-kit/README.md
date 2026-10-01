# ui-kit: the shared look of the console and ADA

The theme, motion, theme store and account-menu pieces that the customer-ops console
(`c:\code\customer-ops\console\web`) and ADA (`C:\code\ADA\frontend`) share, so the two apps stay one
family and each learns from the other. This folder is canonical. Each app holds a generated copy in
`<app>/src/kit/`, stamped `GENERATED from shared-infra/ui-kit -- edit there, then run sync.py`, plus a
`kit.lock.json` with a sha256 per file. Never edit an app's `src/kit/`; edit here and sync.

Started in customer-ops Feature-91 (2026-10-01). Adam decided the theme is per account (per user in
ADA), the kit lives here and is copied with a drift check (not a workspace: the repos are separate), and
both apps adopt it.

## What is in it

| Path | What |
|---|---|
| `css/motion.css` | Feature-79 motion: `--ease-out`, `--ease-in-out`, `--ease-spring`; `animate-pop-in`, `rise-in`, `reveal`, `fade-in`, `sheet-*`, `shimmer`, `pulse-soft`; exits `pop-out`, `fade-out`, `sheet-out-*`; utilities `pressable`, `lift`, `glass`, `stagger-in`; route and theme view transitions; the global `prefers-reduced-motion` stop. Tailwind 4 (`@theme`, `@utility`). |
| `css/themes.css` | Every theme (COPS, Law, Aurora, Sunset, Mint, Tokyo, Wave) in Light, Mid and Dark, on the vocabulary below, plus Wave's decoration. Plain CSS. |
| `theme/themes.ts` | The catalogue: `THEME_IDS`, `MODES`, `THEMES` (label, description, swatch per mode), `THEME_COLOR` (`<meta name="theme-color">`), `schemeOf`, `resolveMode`. |
| `theme/config.ts` | `ThemeKitConfig`, `ThemePrefs`, `ThemeScopeSource`, `prepaintConfig`, `KIT_STORE_VERSION` (4). |
| `theme/prepaint.ts` | `resolveThemeAttrs(raw, rawScope, prefersDark, cfg)` and `buildPrepaintScript(config)`: the no-flash script the app's `vite.config.ts` inlines into `index.html`. |
| `theme/store.ts` | `createThemeStore(config)`, `prefsFor(state, scope, config)`, `readScope(config)`. |
| `theme/ThemeProvider.tsx` | `ThemeProvider({store, config, scope})` and `useTheme()`. |
| `theme/apply.ts`, `theme/storage.ts` | `applyThemeToDocument`, `systemPrefersDark`; `safeLocalStorage`. |
| `theme/testing.ts` | Test helpers for the kit's own tests (`TEST_CONFIG`, `stubColorScheme`). |
| `components/theme-picker.tsx` | `ThemePicker variant="full" \| "compact"`, with an optional `tip` to wrap compact tiles in the app's tooltip. |
| `components/account-menu/` | `AccountMenu`, `AccountMenuTrigger` (dot or avatar, label from sm/md up, `ChevronsUpDown`), `AccountMenuContent`, `AccountMenuHeader` (title, subtitle, meta line, chips), `AccountMenuSection`, `AccountMenuItem`, `AccountMenuSeparator`, `AccountScopeList` (radio items with a check; `unavailable` greys one with its reason), `AccountMenuAppearance` (the compact picker as menu items). |
| `components/theme-dot.tsx`, `segmented.tsx`, `theme-options.tsx`, `cn.ts` | A theme's accent dot; the roving radiogroup; the mode options and swatch gradient; the kit's own class merger. |
| `components/icons.tsx` | The six icons the kit draws (Lucide's paths, inline), so the kit has no icon dependency: ADA refuses `lucide-react` barrel imports (they fetch every icon module in its dev server) and the console has no types for lucide's per-icon paths. |
| `tools/kit-check.mjs` | Synced to `<app>/scripts/kit-check.mjs`: verifies `kit.lock.json` with Node alone. |

Kit files import only `react`, `@radix-ui/react-dropdown-menu`, `zustand`, `clsx`,
`tailwind-merge` and each other by relative path. Never an app's `@/` alias, and no icon package
(`components/icons.tsx`).

## The vocabulary

Every theme block defines these raw custom properties. An app maps them to its utilities in its own
`@theme inline` (the console: `--color-surface-1: var(--surface-1)` and so on) and keeps app-only extras
in its own `index.css`, defined per theme (ADA: `--accent-blue/purple/emerald/amber/rose`, `--pnl-*`).

| Group | Properties |
|---|---|
| Surfaces | `--surface-0/1/2/3`, `--surface-inset` |
| Sidebar | `--sidebar`, `--sidebar-fg`, `--sidebar-muted`, `--sidebar-border`, `--sidebar-hover`, `--sidebar-active`, `--sidebar-active-fg` |
| Text | `--text-primary`, `--text-secondary`, `--text-muted`, `--text-disabled` |
| Border | `--border-subtle`, `--border-strong` |
| Accent | `--accent`, `--accent-fg` (AA text step), `--accent-soft`, `--accent-solid`, `--on-accent`, `--on-danger`, `--ring` |
| Status | `--status-{ok,warn,danger,info}` with `-soft` and `-fg`, `--status-danger-solid`, `--status-neutral-soft` |
| Shadow | `--shadow-1/2/3` |
| Misc | `--ambient-1/2/3`, `--selection-bg`, `--scrollbar-thumb`, `--skeleton-base`, `--skeleton-shine` |
| Type and shape | `--font-display`, `--display-weight`, `--display-tracking`, `--radius` |
| Police | `--siren-red`, `--siren-blue`, `--tape`, `--tape-ink` |

The kit's components also use `text-10/11/13`, `section-label`, `shadow-e1/e3` and `--z-dropdown`, so an
app defines those (the console does in `index.css`).

## Theme per scope

A theme is kept per scope key: the console's account id, ADA's active user id. The store persists

```json
{"state": {"byScope": {"work": {"theme": "law", "mode": "system"}}, "fallback": {"theme": "wave", "mode": "dark"}}, "version": 4}
```

and a scope with no saved choice gets its seed, then the fallback. The app passes one config object to
the store, the provider and the pre-paint script:

```ts
// src/theme/config.ts (no @/ alias: vite.config.ts imports it)
import type { ThemeKitConfig } from '../kit/theme/config';
export const THEME_CONFIG: ThemeKitConfig = {
  storageKey: 'co-theme',                                   // where the store persists
  scope: { storageKey: 'co-account', path: ['state', 'active'] }, // where the current scope key lives
  fallback: { theme: 'wave', mode: 'dark' },
  seeds: { work: { theme: 'law', mode: 'system' }, personal: { theme: 'wave', mode: 'dark' } },
  legacy: (persisted, version) => ..., // the app's pre-kit {theme, mode}, or null; becomes the current scope's
};

// src/theme/store.ts
export const useThemeStore = createThemeStore(THEME_CONFIG);

// the provider, scoped to the current account or user
<ThemeProvider store={useThemeStore} config={THEME_CONFIG} scope={activeId}>...</ThemeProvider>

// vite.config.ts
transformIndexHtml: () => [{ tag: 'script', children: buildPrepaintScript(THEME_CONFIG), injectTo: 'head' }]
```

How each app wires its scope:

- **Console:** `scope: {storageKey: 'co-account', path: ['state', 'active']}` (lib/accountStore.ts);
  `scope={useAccountStore((s) => s.active)}` in `src/theme/AppThemeProvider.tsx`; seeds Work = Law
  (system), Personal = Wave (dark); `legacy` = `consoleLegacyTheme` (its store v1-v3).
- **ADA:** `scope: {storageKey: 'ada.user', path: ['state', 'activeUserId']}` (store/userStore.ts);
  `scope={useUserStore((s) => s.activeUserId)}`; `legacy` reads its old `ada-theme-store` shape
  (`{mode, preset}`, zustand version 0), and `resolveThemeAttrs` reads that flat shape too, so the first load
  after adopting the kit paints the saved preset before the store has migrated; the app also writes the
  user's choice through to `PUT /api/user/preferences/appearance` (ADA `src/theme/sync.ts`).

`ThemeProvider` re-applies on a scope change inside the same view-transition crossfade as a theme pick.
`resolveThemeAttrs` must stay self-contained (its source is inlined); its id lists are repeated inside
it and `prepaint.test.ts` asserts they equal `THEME_IDS`/`MODES`.

## Adding a theme

1. `theme/themes.ts`: the id in `THEME_IDS`, a `THEMES` entry (label, description, a swatch per
   resolved mode) and a `THEME_COLOR` entry per mode.
2. `theme/prepaint.ts`: the same id in the `themes` array inside `resolveThemeAttrs`.
3. `css/themes.css`: `:root[data-theme="<id>"][data-mode="light"]` and a dark block whose selector also
   lists `[data-mode="mid"]`, plus a block in the MID section, defining the whole vocabulary. Every `-fg`
   token needs a measured 4.5:1 ratio against its surfaces, commented "AA step: ..." where it differs
   from the plain token.
4. Each app: its own extras for the new theme in its `index.css` (ADA's accent and P&L families).
5. Sync both apps and run their tests (`prepaint.test.ts`, `store.test.tsx`, `theme-picker.test.tsx`
   run in each app's vitest), then look at every page in the new theme, all three modes, at 400 px.

## Syncing

```
python sync.py <app-frontend-root>          # copy into src/kit/, scripts/kit-check.mjs, write kit.lock.json
python sync.py <app-frontend-root> --check  # exit 1 listing drifted / missing / extra files and a stale lock
```

Python stdlib only, Windows and macOS. Hashes are over LF-normalised text, so a CRLF checkout is not
drift. In each app, `npm run kit:check` (`node scripts/kit-check.mjs`) verifies the lock without
shared-infra present, and is chained into `npm run lint`, so a hand edit to `src/kit/` fails lint, CI
and the pre-commit hook. After changing the kit: sync every app, run its checks, and commit the kit
here and each app's sync in its own repo.
