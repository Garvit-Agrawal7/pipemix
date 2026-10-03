import type { ReactNode } from "react";
import { call } from "./api";
import { useEmit } from "./fader";
import type { SessionState } from "./types";

export interface FootProps {
  state: SessionState;
  disabled: boolean; // nothing connected: master and start/stop are off
  master: number;
  onMaster: (v: number) => void;
  onError: (e: unknown) => void;
  children?: ReactNode;
}

export default function Foot(p: FootProps) {
  const emit = useEmit();
  const live = p.state === "active" || p.state === "repairing";
  const busy = p.state === "starting" || p.state === "stopping";
  const cls = busy || p.disabled ? "btn pri dis" : live ? "btn pri stop" : "btn pri";
  const text =
    p.state === "starting"
      ? "Starting..."
      : p.state === "stopping"
      ? "Stopping..."
      : live
      ? "Stop sharing"
      : "Start sharing";

  const setMaster = (v: number) => {
    p.onMaster(v);
    emit(() => void call("set_master_volume", v).catch(p.onError));
  };

  return (
    <div className="foot">
      <div className={p.disabled ? "mst dim" : "mst"}>
        <div className={p.disabled ? "lvfill off" : "lvfill"} style={{ width: `${p.master}%` }} />
        <div className="lvin">
          <div className="st g mlabel">MASTER</div>
          <div className="grow" />
          <div className="lvnum big">{p.master}</div>
        </div>
        <input
          className="rng"
          type="range"
          min={0}
          max={100}
          value={p.master}
          disabled={p.disabled}
          aria-label="Master volume"
          onChange={(e) => setMaster(e.target.valueAsNumber)}
          onPointerDown={() => setMaster(p.master)} // unmutes even when the value stays put
        />
      </div>
      <div className="acts">
        <button
          className={cls}
          disabled={busy || p.disabled}
          onClick={() => void call(live ? "stop_sharing" : "start_sharing").catch(p.onError)}
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
