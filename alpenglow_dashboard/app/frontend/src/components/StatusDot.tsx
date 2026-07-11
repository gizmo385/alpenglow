import type { Status } from "../api/types";

const STATUS_COLOR: Record<Status, string> = {
  up: "var(--color-ok)",
  down: "var(--color-down)",
  restarting: "var(--color-busy)",
  updating: "var(--color-busy)",
};

type StatusDotProps = {
  status: Status;
  /** Diameter in px. */
  size?: number;
  /** Explicit busy flag; defaults to true for restarting/updating. */
  busy?: boolean;
};

/** Round status dot; softPulse animation while the service is busy. */
export function StatusDot({ status, size = 9, busy }: StatusDotProps) {
  const isBusy = busy ?? (status === "restarting" || status === "updating");
  return (
    <span
      className={`status-dot${isBusy ? " busy" : ""}`}
      style={{
        width: size,
        height: size,
        background: STATUS_COLOR[status],
      }}
    />
  );
}

export { STATUS_COLOR };
