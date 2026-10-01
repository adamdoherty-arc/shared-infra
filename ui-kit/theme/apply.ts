import { THEME_COLOR, schemeOf, type ResolvedMode, type ThemeId } from './themes';

/** Mirror the theme onto <html> exactly as the pre-paint script does. */
export function applyThemeToDocument(theme: ThemeId, resolved: ResolvedMode, doc: Document = document): void {
  const root = doc.documentElement;
  root.setAttribute('data-theme', theme);
  root.setAttribute('data-mode', resolved);
  root.style.colorScheme = schemeOf(resolved);
  let meta = doc.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  if (!meta) {
    meta = doc.createElement('meta');
    meta.name = 'theme-color';
    doc.head.appendChild(meta);
  }
  meta.content = THEME_COLOR[theme][resolved];
}

export function systemPrefersDark(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-color-scheme: dark)').matches
    : false;
}
