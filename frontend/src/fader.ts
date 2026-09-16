/**
 * Shared fader mechanics.
 *
 * In this design a row IS its own volume control, so both the device rows and
 * the master row need the same three things: pointer-x to a percentage, the
 * keyboard equivalents, and a throttle.
 */

import { useEffect, useRef } from "react";
import type { KeyboardEvent } from "react";

export const clamp = (v: number) => Math.max(0, Math.min(100, Math.round(v)));

/** Volume under the pointer, as a percentage of the row's own width. */
export function at(x: number, el: HTMLElement): number {
  const r = el.getBoundingClientRect();
  return clamp(((x - r.left) / r.width) * 100);
}

const STEP: Record<string, number> = {
  ArrowLeft: -1,
  ArrowDown: -1,
  ArrowRight: 1,
  ArrowUp: 1,
  PageDown: -10,
  PageUp: 10,
};

/** The value a key implies, or null when the key means nothing to a slider. */
export function keyValue(e: KeyboardEvent, v: number): number | null {
  const step = STEP[e.key];
  if (step !== undefined) return clamp(v + step);
  if (e.key === "Home") return 0;
  if (e.key === "End") return 100;
  return null;
}

/**
 * pactl forks a process per call, so a drag sends at most one every 60ms,
 * plus a final one on release so the last position always lands.
 */
export function useEmit() {
  const th = useRef({ t: 0, timer: 0 });
  useEffect(() => () => clearTimeout(th.current.timer), []);

  return (fn: () => void, now: boolean) => {
    const s = th.current;
    clearTimeout(s.timer);
    const wait = 60 - (performance.now() - s.t);
    if (now || wait <= 0) {
      s.t = performance.now();
      fn();
    } else {
      s.timer = window.setTimeout(() => {
        s.t = performance.now();
        fn();
      }, wait);
    }
  };
}
