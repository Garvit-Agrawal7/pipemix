import { useEffect, useRef } from "react";

/** pactl forks per call: a drag sends at most one every 60ms, and the last value always
 *  lands, even when another row (key) takes over or the screen unmounts mid-wait. */
export function useEmit() {
  const th = useRef({ t: 0, timer: 0, key: "", pending: null as null | (() => void) });
  useEffect(() => () => {
    clearTimeout(th.current.timer);
    th.current.pending?.();
  }, []);

  return (fn: () => void, key = "") => {
    const s = th.current;
    clearTimeout(s.timer);
    if (s.key !== key) s.pending?.();
    s.key = key;
    s.pending = () => {
      s.t = performance.now();
      s.pending = null;
      fn();
    };
    const wait = 60 - (performance.now() - s.t);
    if (wait <= 0) s.pending();
    else s.timer = window.setTimeout(s.pending, wait);
  };
}
