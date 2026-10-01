/**
 * localStorage behind try/catch: a private window or blocked site data gives null and no-ops, never a
 * crash. The choice still applies for that page view; it just does not persist. Shared by the theme
 * store and any app store that persists the same way (the console's account and shell stores).
 */
import type { StateStorage } from 'zustand/middleware';

export const safeLocalStorage: StateStorage = {
  getItem(name) {
    try {
      return window.localStorage.getItem(name);
    } catch {
      return null;
    }
  },
  setItem(name, value) {
    try {
      window.localStorage.setItem(name, value);
    } catch {
      // Storage full or blocked: the choice still applies for this page view.
    }
  },
  removeItem(name) {
    try {
      window.localStorage.removeItem(name);
    } catch {
      // Blocked storage: nothing to remove.
    }
  },
};
