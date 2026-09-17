declare global {
  interface Window {
    pywebview?: { api: Record<string, (...a: any[]) => Promise<any>> };
    pipemix?: { push: (event: string, payload: any) => void };
  }
}

type Envelope<T> = { ok: true; value: T } | { ok: false; error: string };

export async function call<T>(name: string, ...args: unknown[]): Promise<T> {
  const api = window.pywebview?.api;
  if (!api) throw new Error(`Bridge not ready: ${name}`);
  // A method missing here means the page and Api disagree — almost always a
  // frontend built before the Python side was renamed.
  const fn = api[name];
  if (!fn) throw new Error(`No such bridge method: ${name}. Rebuild the frontend.`);
  const res = (await fn(...args)) as Envelope<T>;
  if (!res?.ok) throw new Error(res?.error ?? `${name} failed`);
  return res.value;
}

export const msg = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function onReady(fn: () => void): void {
  if (window.pywebview?.api) fn();
  else window.addEventListener("pywebviewready", fn, { once: true });
}
