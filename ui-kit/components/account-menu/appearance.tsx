/**
 * AccountMenuAppearance: the compact theme picker as menu items, for inside the account menu.
 *
 * Why not ThemePicker itself: a Radix menu blocks Tab and moves focus only between its own items, so
 * the picker's buttons would be unreachable from the keyboard there. Here each mode and each theme is a
 * menuitemradio in its own group (arrow keys reach every one, aria-checked marks the current), drawn
 * like the compact picker's segments and tiles. Choosing keeps the menu open, so the look can be tried
 * on in place; the change applies to the current scope only.
 */
import * as Menu from '@radix-ui/react-dropdown-menu';
import { cn } from '../../cn';
import { useTheme } from '../../theme/ThemeProvider';
import { THEMES, type ThemeId, type ThemeMode } from '../../theme/themes';
import { MODE_OPTIONS, swatchGradient } from '../theme-options';

const keepOpen = (e: Event) => e.preventDefault();

export function AccountMenuAppearance({ className }: { className?: string }) {
  const { theme, mode, resolved, setTheme, setMode } = useTheme();
  return (
    <div className={cn('flex flex-col gap-2 px-1.5 pb-1.5', className)} data-testid="account-menu-appearance">
      <Menu.RadioGroup
        value={mode}
        onValueChange={(v) => setMode(v as ThemeMode)}
        aria-label="Color mode"
        className="grid grid-cols-4 gap-0.5 rounded-md border border-border-subtle bg-surface-inset p-0.5"
      >
        {MODE_OPTIONS.map((o) => (
          <Menu.RadioItem
            key={o.value}
            value={o.value}
            onSelect={keepOpen}
            className={cn(
              'flex h-7 cursor-pointer items-center justify-center gap-1 rounded-sm text-11 font-medium outline-none select-none [&_svg]:size-3.5',
              'text-text-muted data-[highlighted]:text-text-primary data-[highlighted]:ring-2 data-[highlighted]:ring-ring',
              'data-[state=checked]:bg-surface-1 data-[state=checked]:text-text-primary data-[state=checked]:shadow-e1',
            )}
          >
            {o.icon}
            {o.label}
          </Menu.RadioItem>
        ))}
      </Menu.RadioGroup>
      <Menu.RadioGroup value={theme} onValueChange={(v) => setTheme(v as ThemeId)} aria-label="Theme" className="grid grid-cols-4 gap-1.5">
        {THEMES.map((t) => (
          <Menu.RadioItem
            key={t.id}
            value={t.id}
            onSelect={keepOpen}
            data-testid={`menu-theme-${t.id}`}
            aria-label={`${t.label}: ${t.description}`}
            className={cn(
              'flex cursor-pointer flex-col gap-1 rounded-md border border-border-subtle p-1 outline-none select-none data-[highlighted]:ring-2 data-[highlighted]:ring-ring',
              'bg-surface-1 data-[state=checked]:border-accent data-[state=checked]:bg-accent-soft',
            )}
          >
            <span
              aria-hidden="true"
              data-swatch={t.id}
              className="relative block h-5 w-full overflow-hidden rounded-sm border border-border-subtle"
              style={{ background: swatchGradient(t.swatch[resolved]) }}
            />
            <span aria-hidden="true" className="truncate text-center text-10 font-medium text-text-secondary">
              {t.label}
            </span>
          </Menu.RadioItem>
        ))}
      </Menu.RadioGroup>
    </div>
  );
}
