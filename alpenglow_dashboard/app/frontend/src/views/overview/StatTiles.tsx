/* Overview stat-tile grid (handoff §View 1 stat tiles).
 * repeat(4,1fr) grid: Services, Updates (whole tile → /updates), Monitors,
 * Backups, Host resources (span 2), Storage & ZFS (span 2). Null API fields
 * render "—" so a degraded integration never blanks the tile. */

import { useNavigate } from "react-router-dom";
import { ArrowRightIcon, CheckCircleIcon } from "@phosphor-icons/react";

import type { Overview } from "../../api/types";
import { Card } from "../../components";
import { formatBytes, relativeAgo } from "../../lib/format";
import "./overview.css";

/** "—" for null, else the formatted value. */
function dash(v: string | number | null | undefined): string {
  return v == null || v === "" ? "—" : String(v);
}

/** A labelled 6px bar meter matching the host/storage tile markup. */
function BarMeter({ label, value, pct, color }: {
  label: string;
  value: string;
  pct: number | null;
  color: string;
}) {
  const width = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  return (
    <div className="ov-meter">
      <div className="ov-meter-head">
        <span>{label}</span>
        <span className="ov-meter-value">{value}</span>
      </div>
      <div className="ov-meter-track">
        <div className="ov-meter-fill" style={{ width: `${width}%`, background: color }} />
      </div>
    </div>
  );
}

const MEM_COLOR = "#6bd39a";

function pct(used: number | null, total: number | null): number | null {
  if (used == null || total == null || total === 0) return null;
  return (used / total) * 100;
}

export function StatTiles({ overview }: { overview: Overview | null }) {
  const navigate = useNavigate();

  const svc = overview?.services;
  const up = svc?.up ?? null;
  const total = svc?.total ?? null;
  const down = svc?.down ?? 0;

  const updates = overview?.updates.count ?? null;

  const mon = overview?.monitors;
  const backups = overview?.backups;
  const host = overview?.host;
  const storage = overview?.storage;

  const loadLine =
    host && host.load1 != null
      ? `load ${host.load1?.toFixed(2)} / ${host.load5?.toFixed(2)} / ${host.load15?.toFixed(2)}`
      : "load —";

  const memPct = pct(host?.memUsed ?? null, host?.memTotal ?? null);
  const swapPct = pct(host?.swapUsed ?? null, host?.swapTotal ?? null);

  return (
    <div className="ov-tiles">
      {/* Services */}
      <Card className="ov-tile" elevation="sm">
        <div className="card-kicker">Services</div>
        <div className="ov-tile-big">
          <span className="ov-tile-num">{dash(up)}</span>
          <span className="ov-tile-unit">/ {dash(total)} up</span>
        </div>
        <div className="ov-tile-legend">
          <span className="ov-legend-up">● {dash(up)} running</span>
          {down > 0 && <span className="ov-legend-down">● {down} stopped</span>}
        </div>
      </Card>

      {/* Updates — whole tile clickable */}
      <Card
        className="ov-tile ov-tile-clickable"
        elevation="sm"
        role="button"
        tabIndex={0}
        onClick={() => navigate("/updates")}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            navigate("/updates");
          }
        }}
      >
        <div className="card-kicker">Updates</div>
        <div className="ov-tile-big">
          <span className="ov-tile-num accent">{dash(updates)}</span>
          <span className="ov-tile-unit">available</span>
        </div>
        <div className="ov-tile-action">
          <ArrowRightIcon size={12} /> pull &amp; recreate
        </div>
      </Card>

      {/* Monitors */}
      <Card className="ov-tile" elevation="sm">
        <div className="card-kicker">Monitors</div>
        <div className="ov-tile-big">
          <span className="ov-tile-num">{dash(mon?.up)}</span>
          <span className="ov-tile-unit">/ {dash(mon?.total)} up</span>
        </div>
        <div className="ov-tile-caption">Uptime Kuma · {dash(mon?.note)}</div>
      </Card>

      {/* Backups */}
      <Card className="ov-tile" elevation="sm">
        <div className="card-kicker">Backups</div>
        <div className="ov-tile-big">
          <span className={`ov-tile-num ${backups?.ok ? "ok" : ""}`} style={{ fontSize: 30 }}>
            {backups?.ok == null ? "—" : backups.ok ? "OK" : "FAIL"}
          </span>
        </div>
        <div className="ov-tile-caption">
          pg dump {dash(backups?.pgAgo)} · kopia {dash(backups?.kopiaAgo)}
        </div>
      </Card>

      {/* Host resources (span 2) */}
      <Card className="ov-tile-span2" elevation="sm">
        <div className="ov-tile-head">
          <div className="card-kicker">Host resources</div>
          <div className="ov-tile-load">{loadLine}</div>
        </div>
        <BarMeter
          label="CPU"
          value={host?.cpuPct == null ? "—" : `${Math.round(host.cpuPct)}%`}
          pct={host?.cpuPct ?? null}
          color="var(--color-accent-400)"
        />
        <BarMeter
          label="Memory"
          value={
            host?.memUsed == null
              ? "—"
              : `${formatBytes(host.memUsed)} / ${formatBytes(host.memTotal)}`
          }
          pct={memPct}
          color={MEM_COLOR}
        />
        <BarMeter
          label="Swap"
          value={
            host?.swapUsed == null
              ? "—"
              : `${formatBytes(host.swapUsed)} / ${formatBytes(host.swapTotal)}`
          }
          pct={swapPct}
          color="var(--color-neutral-500)"
        />
      </Card>

      {/* Storage & ZFS (span 2) */}
      <Card className="ov-tile-span2" elevation="sm">
        <div className="ov-tile-head">
          <div className="card-kicker">Storage &amp; ZFS</div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {(storage?.pools ?? []).map((p) => (
              <span key={p.name} className="tag tag-neutral ov-pool-tag">
                <CheckCircleIcon
                  size={12}
                  weight="fill"
                  color={p.state === "ONLINE" ? "#6bd39a" : "var(--color-down)"}
                />
                {p.name} {p.state}
              </span>
            ))}
          </div>
        </div>
        {(storage?.fs ?? []).map((f) => (
          <BarMeter
            key={f.label}
            label={f.label}
            value={
              f.used == null ? "—" : `${formatBytes(f.used)} / ${formatBytes(f.size)}`
            }
            pct={f.pct}
            color="var(--color-neutral-500)"
          />
        ))}
        <div className="ov-tile-caption">
          {storage?.pools?.[0]
            ? `last scrub ${relativeAgo(storage.pools[0].scrubAgo)} · ${
                storage.pools[0].errors ?? 0
              } errors`
            : "—"}
        </div>
      </Card>
    </div>
  );
}
