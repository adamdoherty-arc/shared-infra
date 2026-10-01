/**
 * Account menu primitives: the top-right menu where the account (console) or user (ADA) lives, as in
 * Linear, Vercel, GitHub and Slack. Built on Radix DropdownMenu, so arrow keys, typeahead, Escape and
 * focus return come with it and nothing hand-rolls outside-click or key handling.
 *
 *   <AccountMenu>
 *     <AccountMenuTrigger label="Work" dot={<ThemeDot ... />} aria-label="Account: Work" />
 *     <AccountMenuContent>
 *       <AccountMenuHeader title="Work" subtitle="..." meta="Wave · Claude" chips={...} />
 *       <AccountMenuSection label="Switch account"><AccountScopeList ... /></AccountMenuSection>
 *       <AccountMenuSection label="Appearance for Work"><AccountMenuAppearance /></AccountMenuSection>
 *       <AccountMenuItem asChild><a href="/settings">Settings for Work</a></AccountMenuItem>
 *       <AccountMenuSeparator />
 *       <AccountMenuItem>Sign out</AccountMenuItem>
 *     </AccountMenuContent>
 *   </AccountMenu>
 *
 * The app composes them with its own data, links and router; the kit holds only the shape.
 */
import * as React from 'react';
import * as Menu from '@radix-ui/react-dropdown-menu';
import { Check, ChevronsUpDown } from 'lucide-react';
import { cn } from '../../cn';

export const AccountMenu = Menu.Root;

export interface AccountMenuTriggerProps extends React.ComponentPropsWithoutRef<typeof Menu.Trigger> {
  /** The account or user name. Shown from `labelFrom` up; only the dot (or avatar) below it. */
  label: React.ReactNode;
  /** A ThemeDot or an avatar. */
  dot?: React.ReactNode;
  labelFrom?: 'sm' | 'md';
  'aria-label': string;
}

export const AccountMenuTrigger = React.forwardRef<React.ComponentRef<typeof Menu.Trigger>, AccountMenuTriggerProps>(
  ({ label, dot, labelFrom = 'sm', className, ...props }, ref) => (
    <Menu.Trigger
      ref={ref}
      data-slot="account-menu-trigger"
      className={cn(
        'pressable inline-flex h-8 max-w-48 shrink-0 items-center gap-2 rounded-md border border-border-subtle bg-surface-1 px-2 text-13 font-medium text-text-primary hover:border-border-strong hover:bg-surface-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring data-[state=open]:bg-surface-2',
        className,
      )}
      {...props}
    >
      {dot}
      <span className={cn('hidden min-w-0 truncate', labelFrom === 'md' ? 'md:inline' : 'sm:inline')}>{label}</span>
      <ChevronsUpDown className="size-3.5 shrink-0 text-text-muted" aria-hidden="true" />
    </Menu.Trigger>
  ),
);
AccountMenuTrigger.displayName = 'AccountMenuTrigger';

export const AccountMenuContent = React.forwardRef<
  React.ComponentRef<typeof Menu.Content>,
  React.ComponentPropsWithoutRef<typeof Menu.Content>
>(({ className, align = 'end', sideOffset = 6, collisionPadding = 12, ...props }, ref) => (
  <Menu.Portal>
    <Menu.Content
      ref={ref}
      align={align}
      sideOffset={sideOffset}
      collisionPadding={collisionPadding}
      className={cn(
        'z-[var(--z-dropdown)] flex max-h-[calc(100dvh-5rem)] w-[min(21rem,calc(100vw-2rem))] origin-(--radix-dropdown-menu-content-transform-origin) animate-pop-in flex-col overflow-y-auto rounded-lg border border-border-subtle glass p-1 text-text-primary shadow-e3 focus-visible:outline-none data-[state=closed]:animate-pop-out',
        className,
      )}
      {...props}
    />
  </Menu.Portal>
));
AccountMenuContent.displayName = 'AccountMenuContent';

export interface AccountMenuHeaderProps {
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  /** One short line under the subtitle, e.g. "Wave · local Qwen". */
  meta?: React.ReactNode;
  /** Status chips (writes live/shadow, halted). */
  chips?: React.ReactNode;
}

/** Who this is: not an item, so the arrow keys pass over it. */
export function AccountMenuHeader({ title, subtitle, meta, chips }: AccountMenuHeaderProps) {
  return (
    <Menu.Label className="flex flex-col gap-1 px-2.5 pt-2 pb-2.5" data-slot="account-menu-header">
      <span className="text-sm font-semibold text-text-primary">{title}</span>
      {subtitle && <span className="text-11 leading-snug font-normal text-text-muted">{subtitle}</span>}
      {meta && <span className="text-11 font-medium text-text-secondary">{meta}</span>}
      {chips && <span className="mt-1 flex flex-wrap items-center gap-1.5">{chips}</span>}
    </Menu.Label>
  );
}

export const AccountMenuSeparator = React.forwardRef<
  React.ComponentRef<typeof Menu.Separator>,
  React.ComponentPropsWithoutRef<typeof Menu.Separator>
>(({ className, ...props }, ref) => <Menu.Separator ref={ref} className={cn('my-1 h-px shrink-0 bg-border-subtle', className)} {...props} />);
AccountMenuSeparator.displayName = 'AccountMenuSeparator';

/** A titled group, with a rule above it. */
export function AccountMenuSection({ label, children, className }: { label: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <>
      <AccountMenuSeparator />
      <Menu.Group className={className}>
        <Menu.Label className="section-label px-2.5 pt-1.5 pb-1">{label}</Menu.Label>
        {children}
      </Menu.Group>
    </>
  );
}

const ITEM =
  'flex cursor-pointer items-center gap-2 rounded-md px-2.5 py-2 text-13 outline-none select-none data-[disabled]:cursor-not-allowed data-[disabled]:opacity-50 data-[highlighted]:bg-surface-2 [&_svg]:size-4 [&_svg]:shrink-0';

export const AccountMenuItem = React.forwardRef<
  React.ComponentRef<typeof Menu.Item>,
  React.ComponentPropsWithoutRef<typeof Menu.Item>
>(({ className, ...props }, ref) => <Menu.Item ref={ref} className={cn(ITEM, className)} {...props} />);
AccountMenuItem.displayName = 'AccountMenuItem';

export interface AccountScope {
  id: string;
  label: string;
  /** One line under the label. */
  description?: React.ReactNode;
  /** Why this scope cannot be chosen here; set it and the entry is greyed and inert. */
  unavailable?: React.ReactNode;
  dot?: React.ReactNode;
}

export interface AccountScopeListProps {
  value: string | null;
  scopes: readonly AccountScope[];
  onChoose: (id: string) => void;
  'aria-label': string;
}

/** The accounts (or users), the current one checked. Choosing the current one does nothing. */
export function AccountScopeList({ value, scopes, onChoose, 'aria-label': ariaLabel }: AccountScopeListProps) {
  return (
    <Menu.RadioGroup value={value ?? ''} onValueChange={(id) => id !== value && onChoose(id)} aria-label={ariaLabel}>
      {scopes.map((s) => (
        <Menu.RadioItem
          key={s.id}
          value={s.id}
          disabled={Boolean(s.unavailable)}
          data-testid={`scope-${s.id}`}
          className={cn(ITEM, 'items-start')}
        >
          <span className="flex size-4 shrink-0 items-center justify-center pt-1">{s.dot}</span>
          <span className="flex min-w-0 flex-1 flex-col">
            <span className="font-medium">{s.label}</span>
            {s.unavailable ? (
              <span className="text-11 leading-snug text-text-muted">{s.unavailable}</span>
            ) : (
              s.description && <span className="truncate text-11 text-text-muted">{s.description}</span>
            )}
          </span>
          <Menu.ItemIndicator className="pt-0.5">
            <Check className="text-accent-fg" aria-hidden="true" />
          </Menu.ItemIndicator>
        </Menu.RadioItem>
      ))}
    </Menu.RadioGroup>
  );
}
