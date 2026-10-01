/**
 * ThemeDot: a small round swatch in a theme's accent, so an account (or user) reads as its own look
 * before you open anything: the account menu's trigger and each entry of its switch list.
 */
import { cn } from '../cn';
import { THEMES, type ResolvedMode, type ThemeId } from '../theme/themes';

export interface ThemeDotProps {
  theme: ThemeId;
  resolved: ResolvedMode;
  className?: string;
}

export function ThemeDot({ theme, resolved, className }: ThemeDotProps) {
  const spec = THEMES.find((t) => t.id === theme);
  const [, accent, second] = spec?.swatch[resolved] ?? [];
  return (
    <span
      aria-hidden="true"
      data-theme-dot={theme}
      className={cn('inline-block size-2.5 shrink-0 rounded-full ring-2 ring-surface-0', className)}
      style={accent ? { background: `linear-gradient(135deg, ${accent} 0%, ${accent} 55%, ${second ?? accent} 100%)` } : undefined}
    />
  );
}
