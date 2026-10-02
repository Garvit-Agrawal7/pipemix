import type { ReactNode } from "react";
import { call } from "./api";
import MasterFader from "./MasterFader";
import type { SessionState } from "./types";

export interface FootProps {
  state: SessionState;
  live: boolean;
  disabled: boolean; // nothing connected: master and start/stop are off
  grey: boolean; // also grey the start/stop button while disabled
  master: number;
  onMaster: (v: number) => void;
  onError: (e: unknown) => void;
  children?: ReactNode;
}

export default function Foot(p: FootProps) {
  const busy = p.state === "starting" || p.state === "stopping";
  const cls = busy || (p.grey && p.disabled) ? "btn pri dis" : p.live ? "btn pri stop" : "btn pri";
  const text =
    p.state === "starting"
      ? "Starting..."
      : p.state === "stopping"
      ? "Stopping..."
      : p.live
      ? "Stop sharing"
      : "Start sharing";

  return (
    <div className="foot">
      <MasterFader value={p.master} disabled={p.disabled} onChange={p.onMaster} onError={p.onError} />
      <div className="acts">
        <button
          className={cls}
          disabled={busy || p.disabled}
          onClick={() => void call(p.live ? "stop_sharing" : "start_sharing").catch(p.onError)}
        >
          {text}
        </button>
        {p.children}
        <button className="btn gho" onClick={() => void call("refresh").catch(p.onError)}>
          Refresh
        </button>
      </div>
    </div>
  );
}
