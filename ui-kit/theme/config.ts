/**
 * How an app tells the kit where its theme lives and whose theme it is.
 *
 * A theme is kept PER SCOPE KEY: the console's account id (`work`, `personal`), ADA's active user id.
 * The app names where the current scope key is stored (another localStorage entry and a JSON path
 * into it), so the pre-paint script can resolve the right theme before React has run, and seeds a
 * default per scope so a scope nobody has picked for yet still opens in its own look.
 *
 * The config is plain data apart from `legacy`: vite.config.ts serialises everything else into the
 * inline pre-paint script (`prepaintConfig`), so it must stay JSON-safe.
 */
import type { ThemeId, ThemeMode } from './themes';

export interface ThemePrefs {
  theme: ThemeId;
  mode: ThemeMode;
}

/** Where the app keeps its current scope key: `localStorage[storageKey]`, parsed as JSON, then `path`. */
export interface ThemeScopeSource {
  storageKey: string;
  /** e.g. ['state', 'active'] for a Zustand persist entry `{state: {active: 'work'}, version: 1}`. */
  path: readonly string[];
}

export interface ThemeKitConfig {
  /** The localStorage key the theme store persists under (the console: `co-theme`). */
  storageKey: string;
  /** Where the current scope key lives, or null for one theme per browser. */
  scope: ThemeScopeSource | null;
  /** The theme with no scope, or for a scope with no saved choice and no seed. */
  fallback: ThemePrefs;
  /** Per-scope defaults until that scope saves a choice of its own. */
  seeds: Readonly<Record<string, ThemePrefs>>;
  /**
   * The app's own persisted shape from before the kit (store version < KIT_STORE_VERSION), turned into
   * one {theme, mode}, or null when nothing worth keeping was saved. It becomes the current scope's
   * entry. Runs in the browser only; never serialised into the pre-paint script.
   */
  legacy?: (persisted: unknown, version: number) => ThemePrefs | null;
}

/** The JSON-safe part of the config the pre-paint script receives. */
export type PrepaintConfig = Pick<ThemeKitConfig, 'storageKey' | 'scope' | 'fallback' | 'seeds'>;

export function prepaintConfig(config: ThemeKitConfig): PrepaintConfig {
  return { storageKey: config.storageKey, scope: config.scope, fallback: config.fallback, seeds: config.seeds };
}

/**
 * The store's persisted version. 4 because the console's own store reached 3 before the kit
 * (customer-ops Feature-38, Feature-78), so its saved entries migrate through `legacy`.
 */
export const KIT_STORE_VERSION = 4;
