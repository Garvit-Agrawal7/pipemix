import { useEffect, useState } from "react";
import { call, msg, onReady } from "./api";
import type {
  AudioDevice,
  BackendStatus,
  Preset,
  SessionState,
  Snapshot,
  Stream,
} from "./types";
import { IconApps, IconClose, IconMenu, IconOutputs, IconPower } from "./icons";
import Outputs from "./Outputs";
import Apps from "./Apps";

type Screen = "outputs" | "apps";

export default function App() {
  const [screen, setScreen] = useState<Screen>("outputs");
  const [devices, setDevices] = useState<AudioDevice[]>([]);
  const [state, setState] = useState<SessionState>("idle");
  const [health, setHealth] = useState<BackendStatus>({ health: "ok", message: "" });
  const [master, setMaster] = useState(50);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [preset, setPreset] = useState<string | null>(null);
  const [sink, setSink] = useState<string | null>(null);
  const [streams, setStreams] = useState<Stream[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [quitting, setQuitting] = useState(false);
  const [open, setOpen] = useState(false);

  // Escape collapses the rail; a confirm showing swallows it first.
  useEffect(() => {
    if (!open) return;
    const esc = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (quitting) setQuitting(false);
      else setOpen(false);
    };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [open, quitting]);

  // A collapsed rail has no room to ask, so it never holds a pending confirm.
  useEffect(() => {
    if (!open) setQuitting(false);
  }, [open]);


  // Installed first and never re-installed: it only ever calls setters, so it
  // cannot go stale on state it closed over.
  useEffect(() => {
    window.pipemix = {
      push: (event, payload) => {
        if (event === "devices") {
          setDevices(payload as AudioDevice[]);
        } else if (event === "state") {
          setState(payload as SessionState);
          // The combined sink only exists while a session does.
          call<string | null>("active_sink").then(
            (s) => setSink(s),
            (e) => setError(msg(e)),
          );
        } else if (event === "health") {
          setHealth(payload as BackendStatus);
        } else if (event === "streams") {
          setStreams(payload as Stream[]);
        }
      },
    };
  }, []);

  // The Controller starts after the window, so its first signals can predate
  // the page; the snapshot is what closes that gap.
  useEffect(() => {
    onReady(() => {
      call<Snapshot>("snapshot")
        .then((s) => {
          setDevices(s.devices);
          setState(s.state);
          setHealth(s.health);
          setMaster(s.master);
          setSink(s.sink);
          setPresets(s.presets);
          setPreset(s.preset);
        })
        .catch((e) => setError(msg(e)))
        .finally(() => setReady(true));
      call<Stream[]>("list_streams")
        .then(setStreams)
        .catch((e) => setError(msg(e)));
    });
  }, []);

  return (
    <div className="app">
      <div className={open ? "rail open" : "rail"}>
        <button
          className="railbtn"
          aria-expanded={open}
          aria-label={open ? "Collapse sidebar" : "Expand sidebar"}
          title={open ? "Collapse sidebar" : "Expand sidebar"}
          onClick={() => setOpen(!open)}
        >
          <IconMenu />
          <span className="rlabel rtitle">PipeMix</span>
        </button>

        <button
          className={screen === "outputs" ? "railbtn on" : "railbtn"}
          aria-label="Outputs"
          aria-current={screen === "outputs" ? "page" : undefined}
          title="Outputs"
          onClick={() => setScreen("outputs")}
        >
          <IconOutputs />
          <span className="rlabel">Outputs</span>
        </button>

        <button
          className={screen === "apps" ? "railbtn on" : "railbtn"}
          aria-label={streams.length > 0 ? `Apps, ${streams.length} playing` : "Apps"}
          aria-current={screen === "apps" ? "page" : undefined}
          title="Apps"
          onClick={() => setScreen("apps")}
        >
          <IconApps />
          <span className="rlabel">Apps</span>
          {streams.length > 0 && <span className="badge">{streams.length}</span>}
        </button>

        <div className="spacer" />

        {quitting ? (
          <>
            <button
              className="railbtn danger"
              aria-label="Confirm shut down"
              title="Shut down PipeMix"
              onClick={() => call<null>("shutdown").catch((e) => setError(msg(e)))}
            >
              <IconPower />
              <span className="rlabel">Yes, shut down</span>
            </button>
            <button
              className="railbtn"
              aria-label="Cancel shut down"
              title="Cancel"
              onClick={() => setQuitting(false)}
            >
              <IconClose />
              <span className="rlabel">Cancel</span>
            </button>
          </>
        ) : (
          <button
            className="railbtn"
            aria-label="Shut down PipeMix"
            title="Shut down PipeMix"
            onClick={() => (open ? setQuitting(true) : setOpen(true))}
          >
            <IconPower />
            <span className="rlabel">Shut down</span>
          </button>
        )}
      </div>

      <div className="main">
        {error !== null && (
          <div className="banner" role="alert">
            <div className="grow">{error}</div>
            <button className="bsm" onClick={() => setError(null)}>
              Dismiss
            </button>
          </div>
        )}
        {ready &&
          (screen === "outputs" ? (
            <Outputs
              devices={devices}
              state={state}
              health={health}
              master={master}
              presets={presets}
              preset={preset}
              sink={sink}
              onDevices={setDevices}
              onPresets={setPresets}
              onPreset={setPreset}
              onMaster={setMaster}
              onError={setError}
            />
          ) : (
            <Apps
              devices={devices}
              sink={sink}
              state={state}
              master={master}
              streams={streams}
              onMaster={setMaster}
              onStreams={setStreams}
              onError={setError}
            />
          ))}
      </div>
    </div>
  );
}
