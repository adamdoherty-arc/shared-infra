import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { createThemeStore, prefsFor } from '../theme/store';
import { ThemeProvider } from '../theme/ThemeProvider';
import { stubColorScheme, TEST_CONFIG as CFG } from '../theme/testing';
import { ThemePicker } from './theme-picker';

let scheme: ReturnType<typeof stubColorScheme>;
beforeEach(() => {
  scheme = stubColorScheme(false);
});
afterEach(() => {
  scheme.restore();
  window.localStorage.clear();
});

function setup(variant: 'full' | 'compact' = 'full', scope = 'personal') {
  const useStore = createThemeStore(CFG);
  render(
    <ThemeProvider store={useStore} config={CFG} scope={scope}>
      <ThemePicker variant={variant} />
    </ThemeProvider>,
  );
  return { now: () => prefsFor(useStore.getState(), scope, CFG), useStore };
}

describe('ThemePicker', () => {
  it("renders two radiogroups with the scope's theme and mode checked", () => {
    setup();
    expect(screen.getByRole('radiogroup', { name: 'Theme' })).toBeInTheDocument();
    expect(screen.getByRole('radiogroup', { name: 'Color mode' })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /Wave/ })).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByRole('radio', { name: 'Dark' })).toHaveAttribute('aria-checked', 'true');
  });

  it('has one tab stop per group (roving tabindex)', () => {
    setup();
    const themes = screen.getAllByRole('radio').filter((r) => r.dataset.testid?.startsWith('theme-'));
    expect(themes.filter((r) => r.tabIndex === 0)).toHaveLength(1);
    expect(themes).toHaveLength(7);
  });

  it('arrow keys move focus and select; Home and End jump to the ends', async () => {
    const user = userEvent.setup();
    const { now } = setup('full', 'work');
    const law = screen.getByTestId('theme-law');
    law.focus();
    await user.keyboard('{ArrowRight}');
    expect(now().theme).toBe('aurora');
    expect(screen.getByTestId('theme-aurora')).toHaveFocus();
    expect(screen.getByTestId('theme-aurora')).toHaveAttribute('tabindex', '0');
    expect(law).toHaveAttribute('tabindex', '-1');

    await user.keyboard('{End}');
    expect(now().theme).toBe('wave');
    await user.keyboard('{ArrowRight}');
    expect(now().theme).toBe('cops');
    await user.keyboard('{ArrowLeft}');
    expect(now().theme).toBe('wave');
    await user.keyboard('{Home}');
    expect(now().theme).toBe('cops');
    expect(screen.getByTestId('theme-cops')).toHaveFocus();
  });

  it('mode group works the same way and applies to <html>', async () => {
    const user = userEvent.setup();
    const { now } = setup('full', 'work');
    screen.getByRole('radio', { name: 'Light' }).focus();
    await user.keyboard('{ArrowRight}');
    // Mid sits between light and dark: its own data-mode, a dark color-scheme.
    expect(now().mode).toBe('mid');
    expect(document.documentElement.dataset.mode).toBe('mid');
    expect(document.documentElement.style.colorScheme).toBe('dark');
    await user.keyboard('{ArrowRight}');
    expect(now().mode).toBe('dark');
    await user.keyboard('{Home}');
    expect(now().mode).toBe('system');
  });

  it('clicking a tile selects it for this scope only; compact tiles are named for screen readers', async () => {
    const user = userEvent.setup();
    const { useStore } = setup('compact', 'work');
    await user.click(screen.getByRole('radio', { name: 'Mint' }));
    expect(prefsFor(useStore.getState(), 'work', CFG).theme).toBe('mint');
    expect(prefsFor(useStore.getState(), 'personal', CFG).theme).toBe('wave');
    expect(screen.getByRole('radio', { name: 'Mint' })).toHaveAttribute('aria-checked', 'true');
  });
});
