import { useEffect, useRef, useState } from "react";
import { call, msg } from "./api";
import { at, keyValue, useEmit } from "./fader";
import MasterFader from "./MasterFader";
import type { AudioDevice, BackendStatus, Preset, SessionState } from "./types";
import {
  IconCheck,
  IconChevron,
  IconPlus,
  IconSignal,
  IconSplit,
  IconTrash,
  kindIcon,
} from "./icons";

export interface OutputsProps {
  devices: AudioDevice[];
  state: SessionState;
  health: BackendStatus;
  master: number;
  sink: string | null;
  presets: Preset[];
  preset: string | null;
  onDevices: (d: AudioDevice[]) => void;
  onPresets: (p: Preset[]) => void;
  onPreset: (id: string | null) => void;
  onMaster: (v: number) => void;
  onError: (msg: string) => void;
}

const KIND: Record<string, string> = {
  bluetooth: "Bluetooth",
  usb: "USB",
  hdmi: "HDMI",
  builtin: "Analog",
  unknown: "Audio",
};
type PresetsResult = { presets: Preset[]; preset: string | null };

function meta(d: AudioDevice, recon: boolean): string {
  if (recon) return "Dropped out · PipeMix will pull it back in";
  if (!d.connected) return "Saved · not connected";
  if (d.kind === "bluetooth" && d.battery !== null)
    return `Bluetooth · battery ${d.battery}%`;
  return `${KIND[d.kind] ?? "Audio"} · connected`;
}

export default function Outputs(props: OutputsProps) {
  const { devices, state, health, master, sink, presets, preset } = props;
  const { onDevices, onPresets, onPreset, onMaster, onError } = props;

  const [open, setOpen] = useState(false);
  const [naming, setNaming] = useState(false);
  const [name, setName] = useState("");
  const pop = useRef<HTMLDivElement | null>(null);
  const popBtn = useRef<HTMLButtonElement | null>(null);
  const drag = useRef<string | null>(null);
  const emit = useEmit();

  useEffect(() => {
    if (!open) return;
    const outside = (e: PointerEvent) => {
      const t = e.target as Node;
      if (!pop.current?.contains(t) && !popBtn.current?.contains(t)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  useEffect(() => {
    if (!open) setNaming(false);
  }, [open]);

  const fail = (e: unknown) => onError(msg(e));

  const setDev = (d: AudioDevice, v: number, now = false) => {
    onDevices(devices.map((x) => (x.id === d.id ? { ...x, volume: v } : x)));
    // When it is the only output live, master is the same control, so take
    // back whatever the backend settled on rather than guessing here.
    emit(
      () => void call<number>("set_device_volume", d.id, v).then(onMaster).catch(fail),
      now,
    );
  };

  function keys(e: React.KeyboardEvent, v: number, set: (n: number, now?: boolean) => void) {
    const next = keyValue(e, v);
    if (next === null) return;
    e.preventDefault();
    set(next, true);
  }

  const toggle = (d: AudioDevice) =>
    void call<AudioDevice[]>("toggle_device", d.id, !d.selected)
      .then((ds) => {
        onDevices(ds);
        onPreset(null); // the backend drops the preset the moment a tick is hand-made
      })
      .catch(fail);

  const pick = (id: string) =>
    void call<{ devices: AudioDevice[]; preset: string | null }>("select_preset", id)
      .then((r) => {
        onDevices(r.devices);
        onPreset(r.preset);
        setOpen(false);
      })
      .catch(fail);

  const store = (p: Promise<PresetsResult>) =>
    void p
      .then((r) => {
        onPresets(r.presets);
        onPreset(r.preset);
        setOpen(false);
      })
      .catch(fail);

  const visible = devices.filter((d) => d.connected || (d.target && state === "repairing"));
  const connected = visible.filter((d) => d.connected).length;
  const staged = visible.filter((d) => d.selected).length;
  const liveCount = visible.filter((d) => d.target && d.connected).length;
  const reconCount = visible.filter((d) => d.target && !d.connected).length;
  const live = state === "active" || state === "repairing";
  const busy = state === "starting" || state === "stopping";
  const activePreset = presets.find((p) => p.id === preset);
  const offlineInPreset = activePreset
    ? activePreset.devices.filter((id) => !devices.some((d) => d.id === id && d.connected)).length
    : 0;

  let sub: string;
  if (live) {
    sub = [
      `${liveCount} live`,
      reconCount ? `${reconCount} waiting to rejoin` : "",
      sink ? `combined sink ${sink}` : "",
    ]
      .filter(Boolean)
      .join(" · ");
  } else if (!connected) {
    sub = "No devices connected";
  } else {
    sub = `${connected} connected · ${staged} staged`;
  }

  const priCls = busy || !connected ? "btn pri dis" : live ? "btn pri stop" : "btn pri";
  const priText =
    state === "starting"
      ? "Starting..."
      : state === "stopping"
      ? "Stopping..."
      : live
      ? "Stop sharing"
      : "Start sharing";

  // App owns the single .main; screens contribute its children.
  return (
    <>
      <div className="head">
        <h1 className="h1">Outputs</h1>
        <p className="sub">{sub}</p>
      </div>

      {health.health !== "ok" && (
        <div className="banner">
          <div className="lvico">
            <IconSignal />
          </div>
          <div className="grow">
            <div className="lvnm">
              {health.health === "unavailable"
                ? "The audio server is not answering"
                : "The audio server is having trouble"}
            </div>
            <div className="lvmeta">{health.message}</div>
          </div>
          <button className="bsm" onClick={() => void call("refresh").catch(fail)}>
            Refresh
          </button>
        </div>
      )}

      {health.engine === "leader" && (
        <div className="banner info">
          <div className="lvico">
            <IconSignal />
          </div>
          <div className="grow">
            <div className="lvnm">Running in mirror mode</div>
            <div className="lvmeta">
              Outputs may drift up to 50 ms apart. Install{" "}
              <a href="https://vb-audio.com/Cable/" target="_blank" rel="noreferrer">
                VB-CABLE
              </a>{" "}
              for synced output.
            </div>
          </div>
        </div>
      )}

      <div className="list">
        {offlineInPreset > 0 && (
          <div className="note">
            {offlineInPreset === 1
              ? "1 device in this preset is offline"
              : `${offlineInPreset} devices in this preset are offline`}
          </div>
        )}
        {visible.map((d) => {
          const recon = !d.connected && d.target && state === "repairing";
          const isLive = d.connected && d.target && state === "active";
          const shown = d.connected || recon;
          const Ico = kindIcon(d.kind);
          const border = isLive ? "#2A4038" : recon ? "#3B3A2C" : undefined;
          const scrub = shown
            ? {
                role: "slider",
                tabIndex: 0,
                "aria-label": `${d.name} volume`,
                "aria-valuenow": d.volume,
                "aria-valuemin": 0,
                "aria-valuemax": 100,
                onPointerDown: (e: React.PointerEvent<HTMLDivElement>) => {
                  if (e.button !== 0) return;
                  drag.current = d.id;
                  e.currentTarget.setPointerCapture(e.pointerId);
                  setDev(d, at(e.clientX, e.currentTarget));
                },
                onPointerMove: (e: React.PointerEvent<HTMLDivElement>) => {
                  if (drag.current === d.id) setDev(d, at(e.clientX, e.currentTarget));
                },
                onPointerUp: (e: React.PointerEvent<HTMLDivElement>) => {
                  if (drag.current !== d.id) return;
                  drag.current = null;
                  setDev(d, at(e.clientX, e.currentTarget), true);
                },
                onKeyDown: (e: React.KeyboardEvent) =>
                  keys(e, d.volume, (n, now) => setDev(d, n, now)),
              }
            : {};

          return (
            <div
              key={d.id}
              className={shown ? "lv" : "lv dim"}
              style={border ? { borderColor: border } : undefined}
              {...scrub}
            >
              {shown && (
                <div
                  className={recon ? "lvfill warn" : d.selected ? "lvfill" : "lvfill off"}
                  style={{ width: `${d.volume}%` }}
                />
              )}
              <div className="lvin">
                {/* .st / .st.w only carry the live and warning greens here. */}
                <div className={isLive ? "lvico st" : recon ? "lvico st w" : "lvico"}>
                  <Ico />
                </div>
                <div className="grow">
                  <div className="lvnm">{d.name}</div>
                  <div className="lvmeta">{meta(d, recon)}</div>
                </div>
                {health.engine === "leader" && d.primary && (
                  <div className="st g">PRIMARY</div>
                )}
                {recon ? (
                  <div className="st w">
                    <span className="dot pulse" />
                    RECONNECTING
                  </div>
                ) : !d.connected ? (
                  <div className="st g">OFFLINE</div>
                ) : isLive ? (
                  <div className="st">
                    <span className="dot" />
                    LIVE
                  </div>
                ) : d.selected ? (
                  <div className="st">STAGED</div>
                ) : null}
                <div className={d.selected ? "lvnum" : "lvnum dim"}>
                  {shown ? d.volume : "--"}
                </div>
                <button
                  className={d.selected || recon ? "tg on" : "tg"}
                  disabled={!d.connected}
                  aria-pressed={d.selected || recon}
                  aria-label={`${d.selected ? "Disable" : "Enable"} ${d.name}`}
                  onPointerDown={(e) => e.stopPropagation()}
                  onClick={() => toggle(d)}
                >
                  <i />
                </button>
              </div>
            </div>
          );
        })}

        {!connected && (
          <div className="empty">
            <IconSplit />
            <div className="sub">
              Your presets still work — they'll light up as devices return
            </div>
          </div>
        )}
      </div>

      <div className="foot">
        <MasterFader
          value={master}
          disabled={!connected}
          onChange={onMaster}
          onError={onError}
        />

        <div className="acts">
          <button
            className={priCls}
            disabled={busy || !connected}
            onClick={() =>
              void call(live ? "stop_sharing" : "start_sharing").catch(fail)
            }
          >
            {priText}
          </button>

          <button
            ref={popBtn}
            className="btn sec"
            aria-expanded={open}
            aria-haspopup="menu"
            aria-label={activePreset ? `Presets, ${activePreset.name} selected` : "Presets"}
            onClick={() => setOpen(!open)}
          >
            <span className="ptext">{activePreset ? activePreset.name : "Presets"}</span>
            <IconChevron />
          </button>

          <button className="btn gho" onClick={() => void call("refresh").catch(fail)}>
            Refresh
          </button>

          {open && (
            <div className="pop" ref={pop}>
              {presets.map((p) => (
                <button
                  key={p.id}
                  className={p.id === preset ? "popi sel" : "popi"}
                  onClick={() => pick(p.id)}
                >
                  {p.id === preset ? <IconCheck /> : <svg width="14" height="14" aria-hidden />}
                  {p.name}
                </button>
              ))}
              <div className="popsep" />
              {naming ? (
                <form
                  className="popi"
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (!name.trim()) return;
                    store(call<PresetsResult>("save_preset", name.trim()));
                    setName("");
                  }}
                >
                  <input
                    className="selbox grow"
                    autoFocus
                    value={name}
                    placeholder="Preset name"
                    aria-label="Preset name"
                    onChange={(e) => setName(e.target.value)}
                  />
                </form>
              ) : (
                <button className="popi" onClick={() => setNaming(true)}>
                  <IconPlus />
                  Save this selection
                </button>
              )}
              {activePreset && (
                <button className="popi" style={{ color: "#C97F72" }}
                  onClick={() => store(call<PresetsResult>("delete_preset", activePreset.id))}>
                  <IconTrash />
                  Delete “{activePreset.name}”
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </>
  );
}
