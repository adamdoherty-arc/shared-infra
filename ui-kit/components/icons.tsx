/**
 * The six icons the kit draws, inline: Check, ChevronsUpDown, Monitor, Moon, Sun, SunMoon (Lucide's paths,
 * ISC licence, https://lucide.dev, at lucide-react 0.575; same 24-unit grid, 2px round stroke in
 * currentColor, sized by the caller's classes).
 *
 * Why not `import { Check } from 'lucide-react'`: ADA serves lucide-react un-prebundled in dev, where one
 * barrel import makes the browser fetch every icon module (1,707 of 2,156 requests on one page, measured
 * 2026-09-28), and its pre-commit gate refuses the barrel; the per-icon deep paths ADA uses instead have no
 * type declarations without an app-side tsconfig shim the console does not have. Six inline icons need
 * neither, so the kit has no icon dependency at all (customer-ops Feature-91 / ADA Feature-1001404).
 */
import type { SVGProps } from 'react';
import { cn } from '../cn';

type IconProps = SVGProps<SVGSVGElement>;

function Svg({ className, children, name, ...props }: IconProps & { name: string }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="24"
      height="24"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={cn('lucide', `lucide-${name}`, className)}
      {...props}
    >
      {children}
    </svg>
  );
}

export function CheckIcon(props: IconProps) {
  return (
    <Svg name="check" {...props}>
      <path d="M20 6 9 17l-5-5" />
    </Svg>
  );
}

export function ChevronsUpDownIcon(props: IconProps) {
  return (
    <Svg name="chevrons-up-down" {...props}>
      <path d="m7 15 5 5 5-5" />
      <path d="m7 9 5-5 5 5" />
    </Svg>
  );
}

export function MonitorIcon(props: IconProps) {
  return (
    <Svg name="monitor" {...props}>
      <rect width="20" height="14" x="2" y="3" rx="2" />
      <line x1="8" x2="16" y1="21" y2="21" />
      <line x1="12" x2="12" y1="17" y2="21" />
    </Svg>
  );
}

export function MoonIcon(props: IconProps) {
  return (
    <Svg name="moon" {...props}>
      <path d="M20.985 12.486a9 9 0 1 1-9.473-9.472c.405-.022.617.46.402.803a6 6 0 0 0 8.268 8.268c.344-.215.825-.004.803.401" />
    </Svg>
  );
}

export function SunIcon(props: IconProps) {
  return (
    <Svg name="sun" {...props}>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2" />
      <path d="M12 20v2" />
      <path d="m4.93 4.93 1.41 1.41" />
      <path d="m17.66 17.66 1.41 1.41" />
      <path d="M2 12h2" />
      <path d="M20 12h2" />
      <path d="m6.34 17.66-1.41 1.41" />
      <path d="m19.07 4.93-1.41 1.41" />
    </Svg>
  );
}

export function SunMoonIcon(props: IconProps) {
  return (
    <Svg name="sun-moon" {...props}>
      <path d="M12 2v2" />
      <path d="M14.837 16.385a6 6 0 1 1-7.223-7.222c.624-.147.97.66.715 1.248a4 4 0 0 0 5.26 5.259c.589-.255 1.396.09 1.248.715" />
      <path d="M16 12a4 4 0 0 0-4-4" />
      <path d="m19 5-1.256 1.256" />
      <path d="M20 12h2" />
    </Svg>
  );
}
