declare global {
  interface Window {
    pywebview?: { api: Record<string, (...a: any[]) => Promise<any>> };
    pipemix?: { push: (event: string, payload: any) => void };
  }
}

type Envelope<T> = { ok: true; value: T } | { ok: false; error: string };

export async function call<T>(name: string, ...args: unknown[]): Promise<T> {
  const api = window.pywebview?.api;
  const fn = api?.[name];
  if (!fn) throw new Error(`Bridge not ready: ${name}`);
  const res = (await fn(...args)) as Envelope<T>;
  if (!res?.ok) throw new Error(res?.error ?? `${name} failed`);
  return res.value;
}

export const msg = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function onReady(fn: () => void): void {
  if (window.pywebview?.api) fn();
  else window.addEventListener("pywebviewready", fn, { once: true });
}
