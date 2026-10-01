/**
 * ThemePicker: mode (System / Light / Mid / Dark) and theme (COPS, Law, Aurora, Sunset, Mint, Tokyo,
 * Wave) for the current scope (ADA `ui/theme-picker.tsx` behavior). `variant="full"` backs a settings
 * page; `variant="compact"` fits a popover or a card.
 *
 * Both groups are radiogroups with a roving tab stop: arrow keys move focus and select in one step,
 * Home/End jump to the ends, Tab leaves the group. Inside a menu use AccountMenuAppearance instead: a
 * Radix menu blocks Tab, so only menu items are reachable there.
 *
 * `tip` lets the app wrap each compact tile in its own tooltip (the console's HelpTip) with the theme's
 * description; the kit carries no tooltip of its own, because not every app has Radix Tooltip.
 */
import { useRef, type KeyboardEvent, type ReactElement, type ReactNode } from 'react';
import { CheckIcon } from './icons';
import { cn } from '../cn';
import { useTheme } from '../theme/ThemeProvider';
import { THEMES, type ThemeMode } from '../theme/themes';
import { nextRovingIndex, Segmented } from './segmented';
import { MODE_OPTIONS, swatchGradient } from './theme-options';

export type ThemePickerVariant = 'compact' | 'full';

export interface ThemePickerProps {
  variant?: ThemePickerVariant;
  className?: string;
  tip?: (description: string, tile: ReactElement) => ReactNode;
}

export function ThemePicker({ variant = 'full', className, tip }: ThemePickerProps) {
  const { theme, mode, resolved, setTheme, setMode } = useTheme();
  const refs = useRef<Array<HTMLButtonElement | null>>([]);
  const compact = variant === 'compact';

  function onThemeKey(e: KeyboardEvent<HTMLButtonElement>, idx: number) {
    const next = nextRovingIndex(e.key, idx, THEMES.length);
    if (next === null) return;
    e.preventDefault();
    const t = THEMES[next];
    if (!t) return;
    setTheme(t.id);
    refs.current[next]?.focus();
  }

  return (
    <div className={cn('flex flex-col', compact ? 'gap-3' : 'gap-6', className)} data-testid="theme-picker" data-variant={variant}>
      <div className="flex flex-col gap-2">
        {compact ? <span className="section-label">Mode</span> : <h3 className="section-label">Mode</h3>}
        <Segmented<ThemeMode>
          aria-label="Color mode"
          value={mode}
          onValueChange={setMode}
          options={MODE_OPTIONS}
          size={compact ? 'sm' : 'md'}
          block
          className={compact ? undefined : 'max-w-md'}
        />
        {!compact && (
          <p className="text-xs text-text-muted">
            {mode === 'system'
              ? `Following your system setting (currently ${resolved}).`
              : mode === 'mid'
                ? 'Always mid: slate between light and dark, whatever the system is set to.'
                : `Always ${mode}, whatever the system is set to.`}
          </p>
        )}
      </div>

      <div className="flex flex-col gap-2">
        {compact ? <span className="section-label">Theme</span> : <h3 className="section-label">Theme</h3>}
        <div role="radiogroup" aria-label="Theme" className={cn('grid gap-2', compact ? 'grid-cols-3' : 'grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-7')}>
          {THEMES.map((t, idx) => {
            const active = theme === t.id;
            const tile = (
              <button
                key={t.id}
                ref={(el) => {
                  refs.current[idx] = el;
                }}
                type="button"
                role="radio"
                aria-checked={active}
                aria-label={compact ? t.label : undefined}
                tabIndex={active ? 0 : -1}
                data-testid={`theme-${t.id}`}
                onClick={() => setTheme(t.id)}
                onKeyDown={(e) => onThemeKey(e, idx)}
                className={cn(
                  'group relative flex w-full flex-col text-left pressable focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface-0',
                  compact ? 'gap-1 rounded-md p-1' : 'gap-3 rounded-lg p-3',
                  active ? 'border-2 border-accent bg-accent-soft' : 'border border-border-subtle bg-surface-1 hover:border-border-strong',
                )}
              >
                <span
                  aria-hidden="true"
                  data-swatch={t.id}
                  className={cn('relative block w-full overflow-hidden rounded-sm border border-border-subtle', compact ? 'h-6' : 'h-14')}
                  style={{ background: swatchGradient(t.swatch[resolved]) }}
                />
                {compact ? (
                  <span className="truncate text-center text-10 font-medium text-text-secondary">{t.label}</span>
                ) : (
                  <span className="flex flex-col gap-0.5">
                    <span className="flex items-center gap-1.5 text-sm font-semibold text-text-primary">
                      {t.label}
                      {active && <CheckIcon className="size-3.5 text-accent-fg" aria-hidden="true" />}
                    </span>
                    <span className="text-xs leading-snug text-text-muted">{t.description}</span>
                  </span>
                )}
              </button>
            );
            return compact && tip ? <span key={t.id} className="contents">{tip(t.description, tile)}</span> : tile;
          })}
        </div>
      </div>
    </div>
  );
}
