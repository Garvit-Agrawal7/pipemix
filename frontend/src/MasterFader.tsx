/** The master volume row, shared by both screens because both mockups carry it. */

import { call } from "./api";
import { useEmit } from "./fader";

export interface MasterFaderProps {
  value: number;
  disabled: boolean;
  onChange: (v: number) => void;
  onError: (e: unknown) => void;
}

export default function MasterFader({ value, disabled, onChange, onError }: MasterFaderProps) {
  const emit = useEmit();
  const set = (v: number) => {
    onChange(v);
    emit(() => void call("set_master_volume", v, true).catch(onError));
  };

  return (
    <div className={disabled ? "mst dim" : "mst"}>
      <div className={disabled ? "lvfill off" : "lvfill"} style={{ width: `${value}%` }} />
      <div className="lvin">
        <div className="st g mlabel">MASTER</div>
        <div className="grow" />
        <div className="lvnum big">{value}</div>
      </div>
      <input
        className="rng"
        type="range"
        min={0}
        max={100}
        value={value}
        disabled={disabled}
        aria-label="Master volume"
        onChange={(e) => set(e.target.valueAsNumber)}
        onPointerDown={() => set(value)} // unmutes even when the value stays put
      />
    </div>
  );
}
