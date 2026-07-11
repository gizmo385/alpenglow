type MeterProps = {
  /** Left-hand label (e.g. "CPU · 12 cores"). */
  label: string;
  /** Right-hand mono value (e.g. "41.2 / 64 GB"). */
  value: string;
  /** Fill percentage 0–100. */
  pct: number;
  /** Bar color (defaults to the accent). */
  color?: string;
};

/** label + mono value on one row, a 6px track with a filled bar. */
export function Meter({ label, value, pct, color = "var(--color-accent-400)" }: MeterProps) {
  const width = `${Math.max(0, Math.min(100, pct))}%`;
  return (
    <div className="meter">
      <div className="meter-head">
        <span>{label}</span>
        <span className="meter-value">{value}</span>
      </div>
      <div className="meter-track">
        <div className="meter-fill" style={{ width, background: color }} />
      </div>
    </div>
  );
}
