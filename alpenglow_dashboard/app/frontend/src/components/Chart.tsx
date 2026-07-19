/* Line chart with min/max scale labels and a hover cursor.
 *
 * A step up from <Sparkline>: it draws faint top/bottom gridlines, labels the
 * y-range, and on hover shows a vertical guide + a dot on the series + a tooltip
 * with the value (and time). Pass `min`/`max` to pin the y-axis (e.g. 0–100 for
 * a percentage); omit them to auto-fit the data. Empty data renders a muted "—".
 *
 * The polyline lives in a non-scaling SVG; the cursor/dot/tooltip are positioned
 * in pixels over the container so they stay crisp regardless of width. */

import { useState } from "react";
import type { MouseEvent } from "react";

import type { StatPoint } from "../api/types";
import "./components.css";

const PAD = 5;
const VB_W = 100; // arbitrary viewBox width; stroke is non-scaling

type ChartProps = {
  points: StatPoint[];
  stroke?: string;
  height?: number;
  /** Fixed y-axis bounds; omit to auto-fit the data. */
  min?: number;
  max?: number;
  /** Format for the axis labels and the hover value. */
  formatValue?: (v: number) => string;
  /** Format for the hover time; omit to hide the time row. */
  formatTime?: (t: number) => string;
};

export function Chart({
  points,
  stroke = "var(--color-accent-400)",
  height = 60,
  min,
  max,
  formatValue = (v) => v.toFixed(1),
  formatTime,
}: ChartProps) {
  const [hover, setHover] = useState<{ i: number; w: number } | null>(null);

  if (points.length === 0) {
    return (
      <div className="chart chart-empty" style={{ height }}>
        —
      </div>
    );
  }

  const values = points.map((p) => p.v);
  const lo = min ?? Math.min(...values);
  const hi = max ?? Math.max(...values);
  const span = hi - lo || 1;
  const n = points.length;
  const step = n > 1 ? VB_W / (n - 1) : 0;
  const yFor = (v: number) => PAD + (1 - (v - lo) / span) * (height - PAD * 2);
  const line = values.map((v, i) => `${(i * step).toFixed(2)},${yFor(v).toFixed(2)}`).join(" ");

  const onMove = (e: MouseEvent<HTMLDivElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const i = Math.max(0, Math.min(n - 1, Math.round((x / rect.width) * (n - 1))));
    setHover({ i, w: rect.width });
  };

  const hv = hover ? points[hover.i] : null;
  const cursorX = hover ? (hover.i / Math.max(1, n - 1)) * hover.w : 0;
  const cursorY = hv ? yFor(hv.v) : 0;
  // Keep the tooltip on-screen near the edges.
  const tipShift =
    hover && cursorX < 36 ? "0" : hover && cursorX > hover.w - 36 ? "-100%" : "-50%";

  return (
    <div className="chart" style={{ height }} onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
      <svg
        className="chart-svg"
        viewBox={`0 0 ${VB_W} ${height}`}
        preserveAspectRatio="none"
        style={{ height }}
      >
        <line x1="0" y1={yFor(hi)} x2={VB_W} y2={yFor(hi)} className="chart-grid" vectorEffect="non-scaling-stroke" />
        <line x1="0" y1={yFor(lo)} x2={VB_W} y2={yFor(lo)} className="chart-grid" vectorEffect="non-scaling-stroke" />
        <polyline points={line} fill="none" stroke={stroke} strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
      </svg>

      <span className="chart-axis chart-axis-max">{formatValue(hi)}</span>
      <span className="chart-axis chart-axis-min">{formatValue(lo)}</span>

      {hv && (
        <>
          <div className="chart-cursor" style={{ left: cursorX }} />
          <div className="chart-dot" style={{ left: cursorX, top: cursorY, background: stroke }} />
          <div className="chart-tip" style={{ left: cursorX, transform: `translate(${tipShift}, -100%)` }}>
            <strong>{formatValue(hv.v)}</strong>
            {formatTime && <span>{formatTime(hv.t)}</span>}
          </div>
        </>
      )}
    </div>
  );
}
