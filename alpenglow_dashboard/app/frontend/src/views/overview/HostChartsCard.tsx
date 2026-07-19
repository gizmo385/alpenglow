/* Host trends card (Beszel) — Overview.
 *
 * Four mini charts (CPU %, Memory %, CPU temp °C, Network bandwidth) built from
 * GET /api/host/charts, which Beszel backs. Polls every 30s while the tab is
 * visible. Each chart labels its y-range and reveals exact values on hover.
 * Memory is pinned to 0–100 for absolute context; the rest auto-fit. Empty
 * series (Beszel unconfigured/unreachable) render a muted "—" — never errors. */

import { useEffect, useRef, useState } from "react";

import { api } from "../../api/client";
import type { HostCharts, StatPoint } from "../../api/types";
import { Card, Chart } from "../../components";
import { formatBytes, formatClock } from "../../lib/format";
import "./overview.css";

const POLL_MS = 30_000;

type ChartDef = {
  key: keyof HostCharts;
  label: string;
  color: string;
  /** Format for the latest-value readout, the axis labels, and hover. */
  format: (v: number) => string;
  /** Pin the y-axis; omit to auto-fit. */
  min?: number;
  max?: number;
};

const CHARTS: ChartDef[] = [
  { key: "cpu", label: "CPU", color: "var(--color-accent-400)", format: (v) => `${Math.round(v)}%` },
  { key: "mem", label: "Memory", color: "#6bd39a", format: (v) => `${Math.round(v)}%`, min: 0, max: 100 },
  { key: "temp", label: "CPU temp", color: "#e6a35c", format: (v) => `${Math.round(v)}°C` },
  { key: "bandwidth", label: "Bandwidth", color: "var(--color-neutral-400)", format: (v) => `${formatBytes(v)}/s` },
];

function MiniChart({ def, points }: { def: ChartDef; points: StatPoint[] }) {
  const last = points.length ? points[points.length - 1].v : null;
  return (
    <div className="ov-chart">
      <div className="ov-chart-head">
        <span>{def.label}</span>
        <span className="ov-chart-value">{last == null ? "—" : def.format(last)}</span>
      </div>
      <Chart
        points={points}
        stroke={def.color}
        height={48}
        min={def.min}
        max={def.max}
        formatValue={def.format}
        formatTime={formatClock}
      />
    </div>
  );
}

export function HostChartsCard() {
  const [charts, setCharts] = useState<HostCharts | null>(null);
  const [error, setError] = useState(false);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    const load = async () => {
      try {
        const next = await api.hostCharts();
        if (alive.current) {
          setCharts(next);
          setError(false);
        }
      } catch {
        if (alive.current) setError(true);
      }
    };
    void load();
    const timer = setInterval(load, POLL_MS);
    return () => {
      alive.current = false;
      clearInterval(timer);
    };
  }, []);

  // Degrade quietly: if the endpoint errors outright, or every series is empty
  // (Beszel not configured), don't render a dead card.
  const empty =
    !charts || CHARTS.every((c) => (charts[c.key] as StatPoint[]).length === 0);
  if (error || empty) return null;

  return (
    <Card className="ov-charts" elevation="sm">
      <div className="ov-tile-head">
        <div className="card-kicker">Host trends</div>
        <div className="ov-tile-load">Beszel · last ~2h</div>
      </div>
      <div className="ov-charts-grid">
        {CHARTS.map((def) => (
          <MiniChart key={def.key} def={def} points={charts[def.key] as StatPoint[]} />
        ))}
      </div>
    </Card>
  );
}
