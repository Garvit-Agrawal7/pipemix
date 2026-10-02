import { useRef, useState } from "react";
import { call } from "./api";
import { useEmit } from "./fader";
import Foot from "./Foot";
import type { AudioDevice, BackendStatus, DeviceKind, Preset, SessionState } from "./types";
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
  onError: (e: unknown) => void;
}

export const KIND: Record<DeviceKind, string> = {
  bluetooth: "Bluetooth",
  usb: "USB",
  hdmi: "HDMI",
  builtin: "Analog",
  unknown: "Audio",
};
type Applied = { devices?: AudioDevice[]; presets?: Preset[]; preset: string | null };

function meta(d: AudioDevice, recon: boolean): string {
  if (recon) return "Dropped out · PipeMix will pull it back in";
  return `${KIND[d.kind]} · connected`;
}

export default function Outputs(props: OutputsProps) {
  const { devices, state, health, master, sink, presets, preset } = props;
  const { onDevices, onPresets, onPreset, onMaster, onError } = props;

  const [naming, setNaming] = useState(false);
  const [name, setName] = useState("");
  const pop = useRef<HTMLDivElement>(null);
  const emit = useEmit();

  const setDev = (d: AudioDevice, v: number) => {
    onDevices(devices.map((x) => (x.id === d.id ? { ...x, volume: v } : x)));
    // When it is the only output live, master is the same control, so take
    // back whatever the backend settled on rather than guessing here.
    emit(() => void call<number>("set_device_volume", d.id, v).then(onMaster).catch(onError), d.id);
  };

  const toggle = (d: AudioDevice) => {
    onDevices(devices.map((x) => (x.id === d.id ? { ...x, selected: !d.selected } : x)));
    onPreset(null); // the backend drops the preset the moment a tick is hand-made
    void call<AudioDevice[]>("toggle_device", d.id, !d.selected).then(onDevices).catch(onError);
  };

  const apply = (p: Promise<Applied>) =>
    void p
      .then((r) => {
        if (r.devices) onDevices(r.devices);
        if (r.presets) onPresets(r.presets);
        onPreset(r.preset);
        pop.current?.hidePopover();
      })
      .catch(onError);

  const visible = devices.filter((d) => d.connected || (d.target && state === "repairing"));
  const connected = visible.filter((d) => d.connected).length;
  const staged = visible.filter((d) => d.selected).length;
  const liveCount = visible.filter((d) => d.target && d.connected).length;
  const reconCount = visible.filter((d) => d.target && !d.connected).length;
  const live = state === "active" || state === "repairing";
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
          <button className="bsm" onClick={() => void call("refresh").catch(onError)}>
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
          const recon = !d.connected;
          const isLive = d.connected && d.target && state === "active";
          const Ico = kindIcon(d.kind);
          const border = isLive ? "#2A4038" : recon ? "#3B3A2C" : undefined;

          return (
            <div key={d.id} className="lv" style={border ? { borderColor: border } : undefined}>
              <div
                className={recon ? "lvfill warn" : d.selected ? "lvfill" : "lvfill off"}
                style={{ width: `${d.volume}%` }}
              />
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
                ) : isLive ? (
                  <div className="st">
                    <span className="dot" />
                    LIVE
                  </div>
                ) : d.selected ? (
                  <div className="st">STAGED</div>
                ) : null}
                <div className={d.selected ? "lvnum" : "lvnum dim"}>{d.volume}</div>
                <button
                  className={d.selected || recon ? "tg on" : "tg"}
                  disabled={!d.connected}
                  aria-pressed={d.selected || recon}
                  aria-label={`${d.selected ? "Disable" : "Enable"} ${d.name}`}
                  onClick={() => toggle(d)}
                >
                  <i />
                </button>
              </div>
              <input
                className="rng"
                type="range"
                min={0}
                max={100}
                value={d.volume}
                aria-label={`${d.name} volume`}
                onChange={(e) => setDev(d, e.target.valueAsNumber)}
                onPointerDown={() => setDev(d, d.volume)} // unmutes even when the value stays put
              />
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

      <Foot
        state={state}
        disabled={!connected}
        master={master}
        onMaster={onMaster}
        onError={onError}
      >
        <button
          className="btn sec"
          popovertarget="presets"
          onClick={() => setNaming(false)}
          aria-haspopup="menu"
          aria-label={activePreset ? `Presets, ${activePreset.name} selected` : "Presets"}
        >
          <span className="ptext">{activePreset ? activePreset.name : "Presets"}</span>
          <IconChevron />
        </button>

        <div id="presets" className="pop" popover="auto" ref={pop}>
          {presets.map((p) => (
            <button
              key={p.id}
              className={p.id === preset ? "popi sel" : "popi"}
              onClick={() => apply(call<Applied>("select_preset", p.id))}
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
                apply(call<Applied>("save_preset", name.trim()));
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
            <button
              className="popi"
              style={{ color: "#C97F72" }}
              onClick={() => apply(call<Applied>("delete_preset", activePreset.id))}
            >
              <IconTrash />
              Delete “{activePreset.name}”
            </button>
          )}
        </div>
      </Foot>
    </>
  );
}
