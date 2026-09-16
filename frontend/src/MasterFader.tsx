/** The master volume row, shared by both screens because both mockups carry it. */

import { useRef } from "react";
import { call } from "./api";
import { at, keyValue, useEmit } from "./fader";

export interface MasterFaderProps {
  value: number;
  disabled: boolean;
  onChange: (v: number) => void;
  onError: (msg: string) => void;
}

export default function MasterFader({ value, disabled, onChange, onError }: MasterFaderProps) {
  const emit = useEmit();
  const dragging = useRef(false);

  const set = (v: number, now = false) => {
    onChange(v);
    emit(
      () =>
        void call("set_master_volume", v).catch((e: unknown) =>
          onError(e instanceof Error ? e.message : String(e)),
        ),
      now,
    );
  };

  return (
    <div
      className={disabled ? "mst dim" : "mst"}
      role="slider"
      tabIndex={0}
      aria-label="Master volume"
      aria-valuenow={value}
      aria-valuemin={0}
      aria-valuemax={100}
      onPointerDown={(e) => {
        if (e.button !== 0 || disabled) return;
        dragging.current = true;
        e.currentTarget.setPointerCapture(e.pointerId);
        set(at(e.clientX, e.currentTarget));
      }}
      onPointerMove={(e) => {
        if (dragging.current) set(at(e.clientX, e.currentTarget));
      }}
      onPointerUp={(e) => {
        if (!dragging.current) return;
        dragging.current = false;
        set(at(e.clientX, e.currentTarget), true);
      }}
      onKeyDown={(e) => {
        const next = keyValue(e, value);
        if (next === null || disabled) return;
        e.preventDefault();
        set(next, true);
      }}
    >
      <div className={disabled ? "lvfill off" : "lvfill"} style={{ width: `${value}%` }} />
      <div className="lvin">
        <div className="st g mlabel">MASTER</div>
        <div className="grow" />
        <div className="lvnum big">{value}</div>
      </div>
    </div>
  );
}
