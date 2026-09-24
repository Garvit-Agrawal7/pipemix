import { useEffect, useRef, useState } from "react";
import { call, msg } from "./api";
import MasterFader from "./MasterFader";
import { KIND } from "./Outputs";
import type { AudioDevice, SessionState, Stream } from "./types";
import {
  IconChevron,
  IconMuted,
  IconRefresh,
  IconSignal,
  IconSplit,
  IconVolume,
  IconWave,
  kindIcon,
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

function label(s: Stream, outs: AudioDevice[], live: boolean): string {
  if (!s.devices)
    return live ? "All outputs" : outs.find((d) => d.sink === s.sink)?.name ?? "Default output";
  const names = outs.filter((d) => s.devices!.includes(d.id)).map((d) => d.name);
  if (names.length === 0) return "Offline";
  return names.length <= 2 ? names.join(" + ") : `${names.length} outputs`;
}

export default function Apps(props: AppsProps) {
  const [streams, setStreams] = useState<Stream[]>([]);
  const [err, setErr] = useState("");
  const [open, setOpen] = useState<number | null>(null);
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
        error = msg(e);
      }
      if (!alive) return;
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

  // Nothing picked means following the session.
  const route = async (id: number, ids: string[] | null) => {
    const devices = ids?.length ? ids : null;
    setStreams((prev) => prev.map((s) => (s.id === id ? { ...s, devices } : s)));
    try {
      await call<null>("route_stream", id, devices);
    } catch (e) {
      props.onError(msg(e));
    }
  };

  const mute = async (s: Stream) => {
    setStreams((prev) =>
      prev.map((x) => (x.id === s.id ? { ...x, mute: !s.mute } : x)),
    );
    try {
      await call<null>("set_stream_mute", s.id, !s.mute);
    } catch (e) {
      props.onError(msg(e));
    }
  };

  const outs = props.devices.filter((d) => d.connected && d.sink);
  // An app can only be pointed at what the session is sharing.
  const shared = outs.filter((d) => d.target);
  const hub = props.sink !== null;

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
            const pin = s.devices !== null;
            const on = open === s.id;
            const cur = s.devices ?? (hub ? shared : outs.filter((d) => d.sink === s.sink)).map((d) => d.id);
            const text = shared.length ? label(s, outs, hub) : "No device is sharing";
            return (
              <div className={on ? "lv xp" : "lv"} key={s.id}>
                {pin && <div className="lvfill" style={{ width: 3 }} />}
                {/* The whole header toggles; the button inside is what keyboards reach. */}
                <div className="lvin lvhd" onClick={() => setOpen(on ? null : s.id)}>
                  <div className="lvico">
                    <IconWave />
                  </div>
                  <div className="grow">
                    <div className="lvnm">{s.name}</div>
                    <div className="sid">
                      stream {s.id} · {pin ? "pinned by you" : "following the session"}
                    </div>
                  </div>
                  <button
                    className="selbox selbtn"
                    aria-expanded={on}
                    aria-controls={`outs-${s.id}`}
                    aria-label={`Outputs for ${s.name}: ${text}`}
                  >
                    <span className="t">{text}</span>
                    <IconChevron />
                  </button>
                  <button
                    className={s.mute ? "iconbtn muted" : "iconbtn"}
                    aria-label={s.mute ? `Unmute ${s.name}` : `Mute ${s.name}`}
                    aria-pressed={s.mute}
                    onClick={(e) => {
                      e.stopPropagation();
                      void mute(s);
                    }}
                  >
                    {s.mute ? <IconMuted /> : <IconVolume />}
                  </button>
                </div>
                {on && (
                  <div id={`outs-${s.id}`}>
                    {shared.length === 0 ? (
                      <div className="xrow">
                        <div className="lvnm">No device is sharing</div>
                      </div>
                    ) : (
                      <>
                        <div className="xrow">
                          <div className="grow">
                            <div className="lvnm">Enable all</div>
                          </div>
                          <button
                            className={pin ? "tg" : "tg on"}
                            aria-pressed={!pin}
                            aria-label={`Enable all outputs for ${s.name}`}
                            onClick={() => void route(s.id, pin ? null : cur)}
                          >
                            <i />
                          </button>
                        </div>
                        <div className="hr" />
                      </>
                    )}
                    {shared.map((d) => {
                      const Ico = kindIcon(d.kind);
                      const has = cur.includes(d.id);
                      const next = has ? cur.filter((i) => i !== d.id) : [...cur, d.id];
                      return (
                        <div className="xrow" key={d.id}>
                          <span className="xico">
                            <Ico size={17} />
                          </span>
                          <div className="grow">
                            <div className="lvnm">{d.name}</div>
                            <div className="lvmeta">{KIND[d.kind] ?? "Audio"}</div>
                          </div>
                          <button
                            className={has ? (pin ? "tg on" : "tg on soft") : "tg"}
                            aria-pressed={has}
                            aria-label={`Play ${s.name} on ${d.name}`}
                            onClick={() => void route(s.id, next)}
                          >
                            <i />
                          </button>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })
        )}

        <div className="note">
          <IconRefresh />
          Refreshes every 3 seconds.
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
                onError(msg(e)),
              )
            }
          >
            {state === "starting" ? "Starting..." : state === "stopping" ? "Stopping..." : sessionLive ? "Stop sharing" : "Start sharing"}
          </button>
          <button
            className="btn gho"
            onClick={() =>
              void call("refresh").catch((e: unknown) =>
                onError(msg(e)),
              )
            }
          >
            Refresh
          </button>
        </div>
      </div>
    </>
  );
}
