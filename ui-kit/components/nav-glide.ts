/**
 * nav-glide: the two DOM-written highlights of a sidebar nav (customer-ops Feature-79), shared by every app.
 * useActivePill moves one pill onto the current page's link on a spring; useHoverGlide makes a hover
 * highlight follow the pointer. Both write straight to the elements, so moving them costs no re-render.
 * Pair with the `nav-pill` / `nav-hover` utilities in css/motion.css.
 */
import { useCallback, useLayoutEffect, type PointerEvent as ReactPointerEvent, type RefObject } from 'react';

export const ACTIVE_LINK_SELECTOR = 'a[aria-current="page"]';

export interface ActivePillOptions {
  /** When false the pill is hidden (for example while a drag reorders the links). Default true. */
  enabled?: boolean;
  /** Selector of the active link inside the nav. Default `a[aria-current="page"]`. */
  selector?: string;
}

/**
 * Place `pillRef` over the active link inside `navRef`. Re-placed when `deps` change and when the nav
 * resizes. `data-ready` is set one frame after the first placement so the spring never flies in from the corner.
 */
export function useActivePill(
  navRef: RefObject<HTMLElement | null>,
  pillRef: RefObject<HTMLElement | null>,
  deps: unknown[],
  options: ActivePillOptions = {},
): void {
  const { enabled = true, selector = ACTIVE_LINK_SELECTOR } = options;
  useLayoutEffect(() => {
    const nav = navRef.current;
    const pill = pillRef.current;
    if (!nav || !pill) return;
    const place = () => {
      const link = enabled ? nav.querySelector<HTMLElement>(selector) : null;
      if (!link) {
        pill.style.opacity = '0';
        return;
      }
      const n = nav.getBoundingClientRect();
      const r = link.getBoundingClientRect();
      pill.style.transform = `translate(${r.left - n.left}px, ${r.top - n.top}px)`;
      pill.style.width = `${r.width}px`;
      pill.style.height = `${r.height}px`;
      pill.style.opacity = '1';
      if (!pill.dataset.ready) requestAnimationFrame(() => (pill.dataset.ready = 'true'));
    };
    place();
    const ro = new ResizeObserver(place);
    ro.observe(nav);
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- deps is the caller's re-placement trigger list
  }, [navRef, pillRef, enabled, selector, ...deps]);
}

export interface HoverGlideHandlers {
  onPointerOver: (e: ReactPointerEvent<HTMLElement>) => void;
  onPointerLeave: () => void;
}

/**
 * Pointer-tracking hover highlight: glides from link to link as the pointer moves down the nav and fades
 * out when it leaves. The active link is skipped (the pill already marks it). Spread the result on the nav.
 */
export function useHoverGlide(navRef: RefObject<HTMLElement | null>, hoverRef: RefObject<HTMLElement | null>): HoverGlideHandlers {
  const onPointerOver = useCallback(
    (e: ReactPointerEvent<HTMLElement>) => {
      const nav = navRef.current;
      const hl = hoverRef.current;
      const link = (e.target as HTMLElement).closest<HTMLElement>('a[href]');
      if (!nav || !hl) return;
      if (!link || !nav.contains(link) || link.getAttribute('aria-current') === 'page') {
        hl.style.opacity = '0';
        return;
      }
      const n = nav.getBoundingClientRect();
      const r = link.getBoundingClientRect();
      const hidden = hl.style.opacity !== '1';
      if (hidden) hl.style.transition = 'none';
      hl.style.transform = `translate(${r.left - n.left}px, ${r.top - n.top}px)`;
      hl.style.width = `${r.width}px`;
      hl.style.height = `${r.height}px`;
      if (hidden) {
        void hl.offsetWidth;
        hl.style.transition = '';
      }
      hl.style.opacity = '1';
    },
    [navRef, hoverRef],
  );
  const onPointerLeave = useCallback(() => {
    if (hoverRef.current) hoverRef.current.style.opacity = '0';
  }, [hoverRef]);
  return { onPointerOver, onPointerLeave };
}
