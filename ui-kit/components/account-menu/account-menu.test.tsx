import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createThemeStore, prefsFor } from '../../theme/store';
import { ThemeProvider } from '../../theme/ThemeProvider';
import { stubColorScheme, TEST_CONFIG as CFG } from '../../theme/testing';
import { AccountMenu, AccountMenuAppearance, AccountMenuContent, AccountMenuHeader, AccountMenuItem, AccountMenuSection, AccountMenuTrigger, AccountScopeList } from '.';

let scheme: ReturnType<typeof stubColorScheme>;
beforeEach(() => {
  scheme = stubColorScheme(false);
});
afterEach(() => {
  scheme.restore();
  window.localStorage.clear();
});

function setup(onChoose = vi.fn()) {
  const useStore = createThemeStore(CFG);
  render(
    <ThemeProvider store={useStore} config={CFG} scope="work">
      <AccountMenu>
        <AccountMenuTrigger label="Work" aria-label="Account: Work" />
        <AccountMenuContent>
          <AccountMenuHeader title="Work" subtitle="Customer work" meta="Wave · Claude" />
          <AccountMenuSection label="Switch account">
            <AccountScopeList
              aria-label="Accounts"
              value="work"
              onChoose={onChoose}
              scopes={[
                { id: 'work', label: 'Work' },
                { id: 'personal', label: 'Personal', description: 'Mail and calendar' },
                { id: 'lab', label: 'Lab', unavailable: 'Not served by this console' },
              ]}
            />
          </AccountMenuSection>
          <AccountMenuSection label="Appearance for Work">
            <AccountMenuAppearance />
          </AccountMenuSection>
          <AccountMenuItem>Settings for Work</AccountMenuItem>
        </AccountMenuContent>
      </AccountMenu>
    </ThemeProvider>,
  );
  return { useStore, onChoose };
}

describe('account menu primitives', () => {
  it('lists the scopes with the current one checked and an unavailable one inert', async () => {
    const user = userEvent.setup();
    const { onChoose } = setup();
    await user.click(screen.getByRole('button', { name: 'Account: Work' }));
    expect(screen.getByRole('menuitemradio', { name: /^Work/ })).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByRole('menuitemradio', { name: /Lab/ })).toHaveAttribute('data-disabled');
    await user.click(screen.getByRole('menuitemradio', { name: /Lab/ }));
    expect(onChoose).not.toHaveBeenCalled();
    await user.click(screen.getByRole('menuitemradio', { name: /Personal/ }));
    expect(onChoose).toHaveBeenCalledWith('personal');
  });

  it('picks a theme for the current scope from inside the menu and stays open', async () => {
    const user = userEvent.setup();
    const { useStore } = setup();
    await user.click(screen.getByRole('button', { name: 'Account: Work' }));
    await user.click(screen.getByTestId('menu-theme-tokyo'));
    expect(prefsFor(useStore.getState(), 'work', CFG).theme).toBe('tokyo');
    expect(screen.getByTestId('menu-theme-tokyo')).toHaveAttribute('aria-checked', 'true');
    await user.click(screen.getByRole('menuitemradio', { name: 'Mid' }));
    expect(document.documentElement.dataset.mode).toBe('mid');
    expect(screen.getByRole('menu')).toBeInTheDocument();
  });

  it('reaches every entry from the keyboard', async () => {
    const user = userEvent.setup();
    setup();
    screen.getByRole('button', { name: 'Account: Work' }).focus();
    await user.keyboard('{Enter}');
    // Three accounts, four modes, seven themes, and Settings.
    expect(screen.getAllByRole('menuitemradio')).toHaveLength(3 + 4 + 7);
    expect(screen.getAllByRole('menuitem')).toHaveLength(1);
    await user.keyboard('{ArrowDown}');
    expect(document.activeElement).toBe(screen.getByRole('menuitemradio', { name: /Personal/ }));
    await user.keyboard('{End}');
    expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: 'Settings for Work' }));
  });
});
