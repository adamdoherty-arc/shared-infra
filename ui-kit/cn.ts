/**
 * The kit's own class merger, so kit components never import an app's `@/lib/utils`.
 *
 * tailwind-merge knows Tailwind's default palette and font-size scale, not the kit vocabulary. Left
 * unconfigured, `text-13` (a font-size step) and `text-on-accent` (a color) fall into the SAME unknown
 * text-* group and the later one silently drops the other: customer-ops Fix-35 found every small accent
 * button rendering with no text color at all, ink on brass at 1.65:1 in Law dark. So the vocabulary's
 * color names and the 10/11/13 steps are registered here, as each app also does in its own cn.
 */
import { clsx, type ClassValue } from 'clsx';
import { extendTailwindMerge } from 'tailwind-merge';

const twMerge = extendTailwindMerge({
  extend: {
    theme: {
      color: [
        'surface-0',
        'surface-1',
        'surface-2',
        'surface-3',
        'surface-inset',
        'sidebar',
        'sidebar-fg',
        'sidebar-muted',
        'sidebar-border',
        'sidebar-hover',
        'sidebar-active',
        'sidebar-active-fg',
        'text-primary',
        'text-secondary',
        'text-muted',
        'text-disabled',
        'border-subtle',
        'border-strong',
        'accent',
        'accent-fg',
        'accent-soft',
        'accent-solid',
        'on-accent',
        'ring',
        'status-ok',
        'status-ok-soft',
        'status-ok-fg',
        'status-warn',
        'status-warn-soft',
        'status-warn-fg',
        'status-danger',
        'status-danger-soft',
        'status-danger-fg',
        'status-info',
        'status-info-soft',
        'status-info-fg',
      ],
    },
    classGroups: {
      'font-size': [{ text: ['10', '11', '13'] }],
    },
  },
});

/** Merge class names, letting later Tailwind utilities win over earlier ones. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
