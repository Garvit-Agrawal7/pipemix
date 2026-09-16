import { useEffect, useRef, useState } from "react";
import { call } from "./api";
import MasterFader from "./MasterFader";
import type { AudioDevice, SessionState, Stream } from "./types";
import {
  IconMuted,
  IconRefresh,
  IconSignal,
  IconSplit,
  IconVolume,
  IconWave,
} from "./icons";

export interface AppsProps {
  devices: AudioDevice[];
  sink: string | null;
  state: SessionState;
  master: number;
  onMaster: (v: number) => void;
  onCount: (n: number) => void;
  onError: (msg: string) => void;
}

interface Opt {
  value: string;
  label: string;
}

function routingOptions(devices: AudioDevice[], sink: string | null): Opt[] {
  const opts: Opt[] = devices
    .filter((d) => d.connected && d.sink)
    .map((d) => ({ value: d.sink as string, label: d.name }));
  // The session's combined sink is not a device, so a stream following the
  // session would otherwise have no option matching its own sink.
  if (sink && !opts.some((o) => o.value === sink))
    opts.unshift({ value: sink, label: "All outputs (combined)" });
  return opts;
}

const text = (e: unknown) => (e instanceof Error ? e.message : String(e));

export default function Apps(props: AppsProps) {
  const [streams, setStreams] = useState<Stream[]>([]);
  const [err, setErr] = useState("");
  const [pinned, setPinned] = useState<number[]>([]);
  const sig = useRef("");
  const live = useRef(props);
  live.current = props;

  const { master, state, onMaster, onError } = props;
  const sessionLive = state === "active";
  const busy = state === "starting" || state === "stopping";
  const anyConnected = props.devices.some((d) => d.connected);

  useEffect(() => {
    let alive = true;
    const refresh = async () => {
      let next: Stream[] = [];
      let error = "";
      try {
        next = await call<Stream[]>("list_streams");
      } catch (e) {
        error = text(e);
      }
      if (!alive) return;
      const { devices, sink } = live.current;
      const now = JSON.stringify([error, next, routingOptions(devices, sink)]);
      // A refresh that changes nothing must not re-render: it would close an
      // open <select> under the user's cursor.
      if (now === sig.current) return;
      sig.current = now;
      setErr(error);
      setStreams(next);
    };
    void refresh();
    const timer = setInterval(refresh, 3000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    live.current.onCount(streams.length);
  }, [streams.length]);

  const route = async (id: number, sink: string) => {
    setStreams((prev) => prev.map((s) => (s.id === id ? { ...s, sink } : s)));
    setPinned((prev) => (prev.includes(id) ? prev : [...prev, id]));
    try {
      await call<null>("route_stream", id, sink);
    } catch (e) {
      props.onError(text(e));
    }
  };

  const mute = async (s: Stream) => {
    setStreams((prev) =>
      prev.map((x) => (x.id === s.id ? { ...x, mute: !s.mute } : x)),
    );
    try {
      await call<null>("set_stream_mute", s.id, !s.mute);
    } catch (e) {
      props.onError(text(e));
    }
  };

  const opts = routingOptions(props.devices, props.sink);

  return (
    <>
      <div className="head">
        <h1 className="h1">Apps</h1>
        <p className="sub">
          {streams.length} playing · anything you route by hand stays put when
          outputs change
        </p>
      </div>

      {err && (
        <div className="banner" role="alert">
          <IconSignal />
          <div className="grow">{err}</div>
        </div>
      )}

      <div className="list">
        {err || streams.length === 0 ? (
          <div className="empty">
            <IconSplit />
            <div>Nothing is playing right now</div>
          </div>
        ) : (
          streams.map((s) => {
            const pin = pinned.includes(s.id);
            return (
              <div className="lv" key={s.id}>
                {pin && <div className="lvfill" style={{ width: 3 }} />}
                <div className="lvin">
                  <div className="lvico">
                    <IconWave />
                  </div>
                  <div className="grow">
                    <div className="lvnm">{s.name}</div>
                    <div className="sid">
                      stream {s.id} · {pin ? "pinned by you" : "following the session"}
                    </div>
                  </div>
                  <select
                    className="selbox"
                    aria-label={`Output for ${s.name}`}
                    value={s.sink}
                    onChange={(e) => void route(s.id, e.target.value)}
                  >
                    {!opts.some((o) => o.value === s.sink) && (
                      <option value={s.sink}>{s.sink}</option>
                    )}
                    {opts.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                  <button
                    className={s.mute ? "iconbtn muted" : "iconbtn"}
                    aria-label={s.mute ? `Unmute ${s.name}` : `Mute ${s.name}`}
                    aria-pressed={s.mute}
                    onClick={() => void mute(s)}
                  >
                    {s.mute ? <IconMuted /> : <IconVolume />}
                  </button>
                </div>
              </div>
            );
          })
        )}

        <div className="note">
          <IconRefresh />
          Refreshes every 3 seconds. An open dropdown is never closed by a
          refresh.
        </div>
      </div>

      <div className="foot">
        <MasterFader
          value={master}
          disabled={!anyConnected}
          onChange={onMaster}
          onError={onError}
        />
        <div className="acts">
          <button
            className={sessionLive ? "btn pri stop" : busy ? "btn pri dis" : "btn pri"}
            disabled={busy || !anyConnected}
            onClick={() =>
              void call(sessionLive ? "stop_sharing" : "start_sharing").catch((e: unknown) =>
                onError(e instanceof Error ? e.message : String(e)),
              )
            }
          >
            {state === "starting" ? "Starting..." : state === "stopping" ? "Stopping..." : sessionLive ? "Stop sharing" : "Start sharing"}
          </button>
          <button
            className="btn gho"
            onClick={() =>
              void call("reset_audio").catch((e: unknown) =>
                onError(e instanceof Error ? e.message : String(e)),
              )
            }
          >
            Reset audio
          </button>
        </div>
      </div>
    </>
  );
}
