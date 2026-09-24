// Hand-mirrored from src/pipemix/models.py and services/backend/__init__.py.
// Enums cross the bridge as their .value string (bridge.to_json).

export type DeviceKind = "bluetooth" | "usb" | "hdmi" | "builtin" | "unknown";

export type SessionState =
  | "idle"
  | "starting"
  | "active"
  | "repairing"
  | "stopping"
  | "error";

export type BackendHealth = "ok" | "unavailable" | "degraded";

export interface AudioDevice {
  id: string;
  name: string;
  sink: string | null; // null while known but disconnected
  kind: DeviceKind;
  connected: boolean;
  battery: number | null;
  volume: number;
  // Added by Api.devices_payload, not on the dataclass.
  selected: boolean;
  target: boolean; // in the live session, even if it dropped out
  primary: boolean; // the elected leader in leader mode; meaningless otherwise
}

export interface BackendStatus {
  health: BackendHealth;
  message: string;
  // "native" | "hub" | "leader" — set by WasapiBackend.health(); absent only
  // from App.tsx's placeholder state before the first snapshot lands.
  engine?: string;
}

export interface Stream {
  id: number;
  name: string;
  sink: string;
  mute: boolean;
}

export interface Preset {
  id: string;
  name: string;
  devices: string[];
}

export interface Snapshot {
  devices: AudioDevice[];
  state: SessionState;
  health: BackendStatus;
  master: number;
  sink: string | null;
  presets: Preset[];
  preset: string | null;
}
