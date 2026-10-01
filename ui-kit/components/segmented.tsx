/**
 * Segmented: a keyboard-accessible one-of-N switch (ADA `ui/segmented.tsx`), the ThemePicker's mode
 * row. Roving tabindex with role="radiogroup": arrows move focus and selection together, Home/End jump
 * to the ends.
 */
import { useRef, type KeyboardEvent, type ReactNode } from 'react';
import { cn } from '../cn';

export interface SegmentedOption<T extends string = string> {
  value: T;
  label: ReactNode;
  icon?: ReactNode;
  disabled?: boolean;
}

export interface SegmentedProps<T extends string = string> {
  value: T;
  onValueChange: (value: T) => void;
  options: readonly SegmentedOption<T>[];
  size?: 'sm' | 'md';
  /** Stretch to the container width with equal segments. */
  block?: boolean;
  className?: string;
  'aria-label': string;
}

/** Next index for a roving-focus key, or null when the key is not a navigation key. */
// eslint-disable-next-line react-refresh/only-export-components -- shared by ThemePicker
export function nextRovingIndex(key: string, idx: number, count: number): number | null {
  if (count <= 0) return null;
  if (key === 'ArrowRight' || key === 'ArrowDown') return (idx + 1) % count;
  if (key === 'ArrowLeft' || key === 'ArrowUp') return (idx - 1 + count) % count;
  if (key === 'Home') return 0;
  if (key === 'End') return count - 1;
  return null;
}

export function Segmented<T extends string = string>({
  value,
  onValueChange,
  options,
  size = 'md',
  block = false,
  className,
  'aria-label': ariaLabel,
}: SegmentedProps<T>) {
  const refs = useRef<Array<HTMLButtonElement | null>>([]);
  const enabled = options.map((o, i) => (o.disabled ? -1 : i)).filter((i) => i >= 0);
  const selectedIdx = options.findIndex((o) => o.value === value);
  const tabStop = selectedIdx >= 0 && !options[selectedIdx]?.disabled ? selectedIdx : enabled[0];

  function onKeyDown(e: KeyboardEvent<HTMLButtonElement>, index: number) {
    const pos = enabled.indexOf(index);
    const next = nextRovingIndex(e.key, pos, enabled.length);
    if (next === null) return;
    e.preventDefault();
    const target = enabled[next];
    if (target === undefined) return;
    const opt = options[target];
    if (!opt) return;
    refs.current[target]?.focus();
    onValueChange(opt.value);
  }

  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      className={cn(
        'items-center gap-0.5 rounded-md border border-border-subtle bg-surface-inset p-0.5',
        block ? 'grid auto-cols-fr grid-flow-col' : 'inline-flex',
        className,
      )}
    >
      {options.map((opt, i) => {
        const selected = opt.value === value;
        return (
          <button
            key={opt.value}
            ref={(el) => {
              refs.current[i] = el;
            }}
            type="button"
            role="radio"
            aria-checked={selected}
            disabled={opt.disabled}
            tabIndex={i === tabStop ? 0 : -1}
            onClick={() => !opt.disabled && onValueChange(opt.value)}
            onKeyDown={(e) => onKeyDown(e, i)}
            className={cn(
              'inline-flex items-center justify-center gap-1.5 rounded-sm font-medium whitespace-nowrap pressable focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring [&_svg]:size-3.5',
              size === 'sm' ? 'h-7 px-2.5 text-xs' : 'h-8 px-3 text-13',
              selected
                ? 'bg-surface-1 text-text-primary shadow-e1 ring-1 ring-border-strong'
                : 'text-text-muted hover:text-text-primary',
              opt.disabled && 'cursor-not-allowed opacity-50',
            )}
          >
            {opt.icon}
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}
