import { useEffect, useRef, useState } from "react";
import { call, onReady } from "./api";
import type {
  AudioDevice,
  BackendStatus,
  Preset,
  SessionState,
  Snapshot,
} from "./types";
import { IconApps, IconChevron, IconMenu, IconOutputs, IconPower } from "./icons";
import Outputs from "./Outputs";
import Apps from "./Apps";

type Screen = "outputs" | "apps";

const msg = (e: unknown) => (e instanceof Error ? e.message : String(e));

export default function App() {
  const [screen, setScreen] = useState<Screen>("outputs");
  const [devices, setDevices] = useState<AudioDevice[]>([]);
  const [state, setState] = useState<SessionState>("idle");
  const [health, setHealth] = useState<BackendStatus>({
    health: "ok",
    message: "",
    details: "",
  });
  const [master, setMaster] = useState(50);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [preset, setPreset] = useState<string | null>(null);
  const [sink, setSink] = useState<string | null>(null);
  const [streamCount, setStreamCount] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [quitting, setQuitting] = useState(false);
  const [menu, setMenu] = useState(false);
  const [menuQuit, setMenuQuit] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const menuBtn = useRef<HTMLButtonElement | null>(null);

  // The rail is icon-only, so the menu is where each destination is named.
  useEffect(() => {
    if (!menu) return;
    const outside = (e: PointerEvent) => {
      const t = e.target as Node;
      if (!menuRef.current?.contains(t) && !menuBtn.current?.contains(t)) setMenu(false);
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenu(false);
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", esc);
    };
  }, [menu]);

  useEffect(() => {
    if (!menu) setMenuQuit(false);
  }, [menu]);

  const go = (s: Screen) => {
    setScreen(s);
    setMenu(false);
  };


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
    });
  }, []);

  return (
    <div className="app">
      <div className="rail">
        <button
          ref={menuBtn}
          className="railbtn"
          aria-label="Menu"
          aria-haspopup="menu"
          aria-expanded={menu}
          title="Menu"
          onClick={() => setMenu(!menu)}
        >
          <IconMenu />
        </button>
        <button
          className={screen === "outputs" ? "railbtn on" : "railbtn"}
          aria-label="Outputs"
          aria-current={screen === "outputs" ? "page" : undefined}
          title="Outputs"
          onClick={() => setScreen("outputs")}
        >
          <IconOutputs />
        </button>
        <button
          className={screen === "apps" ? "railbtn on" : "railbtn"}
          aria-label={streamCount > 0 ? `Apps, ${streamCount} playing` : "Apps"}
          aria-current={screen === "apps" ? "page" : undefined}
          title="Apps"
          onClick={() => setScreen("apps")}
        >
          <IconApps />
          {streamCount > 0 && <span className="badge">{streamCount}</span>}
        </button>
        <div className="spacer" />
        {quitting ? (
          <>
            <button
              className="iconbtn muted"
              aria-label="Confirm quit"
              title="Quit PipeMix"
              onClick={() => call<null>("shutdown").catch((e) => setError(msg(e)))}
            >
              <IconPower size={17} />
            </button>
            <button
              className="iconbtn"
              aria-label="Cancel quit"
              title="Cancel"
              onClick={() => setQuitting(false)}
            >
              <IconChevron />
            </button>
          </>
        ) : (
          <button
            className="railbtn"
            aria-label="Quit PipeMix"
            title="Quit PipeMix"
            onClick={() => setQuitting(true)}
          >
            <IconPower />
          </button>
        )}

        {menu && (
          <div className="menu" ref={menuRef} role="menu" aria-label="PipeMix">
            <button
              role="menuitem"
              className={screen === "outputs" ? "popi sel" : "popi"}
              onClick={() => go("outputs")}
            >
              <IconOutputs size={17} />
              Outputs
            </button>
            <button
              role="menuitem"
              className={screen === "apps" ? "popi sel" : "popi"}
              onClick={() => go("apps")}
            >
              <IconApps size={17} />
              Apps
              {streamCount > 0 && <span className="mcount">{streamCount}</span>}
            </button>
            <div className="popsep" />
            {menuQuit ? (
              <>
                <button
                  role="menuitem"
                  className="popi"
                  style={{ color: "#C97F72" }}
                  onClick={() => call<null>("shutdown").catch((e) => setError(msg(e)))}
                >
                  <IconPower size={17} />
                  Yes, shut down
                </button>
                <button role="menuitem" className="popi" onClick={() => setMenuQuit(false)}>
                  <span style={{ width: 17 }} />
                  Cancel
                </button>
              </>
            ) : (
              <button
                role="menuitem"
                className="popi"
                style={{ color: "#C97F72" }}
                onClick={() => setMenuQuit(true)}
              >
                <IconPower size={17} />
                Shut down PipeMix
              </button>
            )}
          </div>
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
              onMaster={setMaster}
              onCount={setStreamCount}
              onError={setError}
            />
          ))}
      </div>
    </div>
  );
}
