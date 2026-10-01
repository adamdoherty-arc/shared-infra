/**
 * What the ThemePicker and the account menu's appearance section both draw from: the four modes with
 * their icons, and the three-stop swatch gradient behind a theme tile.
 */
import type { ReactNode } from 'react';
import { MonitorIcon, MoonIcon, SunIcon, SunMoonIcon } from './icons';
import type { ThemeMode } from '../theme/themes';

export const MODE_OPTIONS: { value: ThemeMode; label: string; icon: ReactNode }[] = [
  { value: 'system', label: 'System', icon: <MonitorIcon aria-hidden="true" /> },
  { value: 'light', label: 'Light', icon: <SunIcon aria-hidden="true" /> },
  { value: 'mid', label: 'Mid', icon: <SunMoonIcon aria-hidden="true" /> },
  { value: 'dark', label: 'Dark', icon: <MoonIcon aria-hidden="true" /> },
];

/** The three-stop swatch behind a theme tile. */
export function swatchGradient(stops: readonly [string, string, string]): string {
  const [a, b, c] = stops;
  return `linear-gradient(135deg, ${a} 0%, ${b} 55%, ${c} 100%)`;
}
