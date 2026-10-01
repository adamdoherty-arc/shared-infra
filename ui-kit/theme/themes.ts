/**
 * The theme catalogue. Swatch and theme-color values are the only color literals outside the CSS,
 * which is why the kit's theme/ folder is exempt from each app's no-hardcoded-color lint rule.
 * Which theme an app or a scope opens in is not decided here but by the app's ThemeKitConfig
 * (config.ts): the console opens Work in Law and Personal in Wave, ADA has its own seeds.
 */
export const THEME_IDS = ['cops', 'law', 'aurora', 'sunset', 'mint', 'tokyo', 'wave'] as const;
export type ThemeId = (typeof THEME_IDS)[number];

/** Mid (Feature-79): a slate between light and dark, never picked by "system" (the OS only has two). */
export const MODES = ['system', 'light', 'mid', 'dark'] as const;
export type ThemeMode = (typeof MODES)[number];
export type ResolvedMode = 'light' | 'mid' | 'dark';

/** The CSS color-scheme (and toast theme) for a resolved mode: mid is dark-schemed, light text on slate. */
export function schemeOf(resolved: ResolvedMode): 'light' | 'dark' {
  return resolved === 'light' ? 'light' : 'dark';
}

export interface ThemeSpec {
  id: ThemeId;
  label: string;
  description: string;
  /** Three stops for the picker swatch: surface, accent, a second accent. Per resolved mode. */
  swatch: Record<ResolvedMode, readonly [string, string, string]>;
}

export const THEMES: readonly ThemeSpec[] = [
  {
    id: 'cops',
    label: 'COPS',
    description: 'Night-shift navy, police blue and siren red',
    swatch: { light: ['#eef1f8', '#1d4ed8', '#dc2626'], mid: ['#364563', '#81b8fb', '#fca5a5'], dark: ['#070b16', '#3b82f6', '#ef4444'] },
  },
  {
    id: 'law',
    label: 'Law',
    description: 'Ivory, ink and brass; navy chamber walls',
    swatch: { light: ['#faf7ef', '#a17d4a', '#1a1a2e'], mid: ['#444a5a', '#deba7d', '#373b49'], dark: ['#10141e', '#cea969', '#090d16'] },
  },
  {
    id: 'aurora',
    label: 'Aurora',
    description: 'Blue, violet and emerald',
    swatch: { light: ['#3b82f6', '#8b5cf6', '#10b981'], mid: ['#3b82f6', '#8b5cf6', '#10b981'], dark: ['#3b82f6', '#8b5cf6', '#10b981'] },
  },
  {
    id: 'sunset',
    label: 'Sunset',
    description: 'Warm amber, rose and gold',
    swatch: { light: ['#f97316', '#ec4899', '#fbbf24'], mid: ['#f97316', '#ec4899', '#fbbf24'], dark: ['#f97316', '#ec4899', '#fbbf24'] },
  },
  {
    id: 'mint',
    label: 'Mint',
    description: 'Cool cyan, teal and emerald',
    swatch: { light: ['#06b6d4', '#14b8a6', '#34d399'], mid: ['#06b6d4', '#14b8a6', '#34d399'], dark: ['#06b6d4', '#14b8a6', '#34d399'] },
  },
  {
    id: 'tokyo',
    label: 'Tokyo',
    description: 'After-hours indigo and magenta neon',
    swatch: { light: ['#6366f1', '#d946ef', '#f43f5e'], mid: ['#6366f1', '#d946ef', '#f43f5e'], dark: ['#6366f1', '#d946ef', '#f43f5e'] },
  },
  {
    id: 'wave',
    label: 'Wave',
    description: 'Ocean swell: abyss navy, aqua, seafoam and sand',
    swatch: { light: ['#0e7490', '#22d3ee', '#f6dca6'], mid: ['#2e4e5e', '#22d3ee', '#5eead4'], dark: ['#041521', '#22d3ee', '#5eead4'] },
  },
];

/** <meta name="theme-color">: each theme's --surface-0 per mode (css/themes.css). */
export const THEME_COLOR: Record<ThemeId, Record<ResolvedMode, string>> = {
  cops: { light: '#eef1f8', mid: '#364563', dark: '#05070d' },
  law: { light: '#faf7ef', mid: '#444a5a', dark: '#10141e' },
  aurora: { light: '#f5f7fb', mid: '#394960', dark: '#0a0e1a' },
  sunset: { light: '#f5f7fb', mid: '#394960', dark: '#0a0e1a' },
  mint: { light: '#f5f7fb', mid: '#394960', dark: '#0a0e1a' },
  tokyo: { light: '#f5f7fb', mid: '#394960', dark: '#0a0e1a' },
  wave: { light: '#eef8fb', mid: '#2e4e5e', dark: '#041521' },
};

/** A stored mode resolved against the OS preference: system picks only light or dark, never mid. */
export function resolveMode(mode: ThemeMode, systemDark: boolean): ResolvedMode {
  return mode === 'system' ? (systemDark ? 'dark' : 'light') : mode;
}
