/* Per-container Beszel charts (detail → Resources tab).
 *
 * For each of the service's containers, Beszel's recent CPU % and memory history
 * as two hover-able charts with a live value readout. Polls every 15s while
 * mounted. Renders nothing when Beszel is unconfigured/unreachable or none of
 * the service's containers are tracked — the tab keeps the docker-stats cards. */

import { useEffect, useRef, useState } from "react";

import { api } from "../../api/client";
import type { BeszelContainer } from "../../api/types";
import { Card, Chart } from "../../components";
import { formatClock } from "../../lib/format";
import css from "./detail.module.css";

const POLL_MS = 15_000;

/** Beszel memory is mebibytes; show whole MB, or GB once large. */
function mem(mib: number | null): string {
  if (mib == null) return "—";
  return mib >= 1024 ? `${(mib / 1024).toFixed(1)} GB` : `${Math.round(mib)} MB`;
}

function cpu(pct: number | null): string {
  return pct == null ? "—" : `${pct.toFixed(1)}%`;
}

function ContainerBlock({ c }: { c: BeszelContainer }) {
  return (
    <div className={css.beszelBlock}>
      <div className={css.beszelBlockHead}>
        <span className={css.beszelName}>{c.name}</span>
        <span className={css.beszelStatus}>{c.status ?? "—"}</span>
      </div>
      <div className={css.beszelCharts}>
        <div className={css.beszelChart}>
          <div className={css.beszelChartHead}>
            <span>CPU</span>
            <span className={css.beszelChartValue}>{cpu(c.cpu)}</span>
          </div>
          <Chart
            points={c.cpuHistory}
            stroke="var(--color-accent-400)"
            height={44}
            formatValue={(v) => `${v.toFixed(1)}%`}
            formatTime={formatClock}
          />
        </div>
        <div className={css.beszelChart}>
          <div className={css.beszelChartHead}>
            <span>Memory</span>
            <span className={css.beszelChartValue}>{mem(c.memory)}</span>
          </div>
          <Chart
            points={c.memHistory}
            stroke="#6bd39a"
            height={44}
            formatValue={mem}
            formatTime={formatClock}
          />
        </div>
      </div>
    </div>
  );
}

export function BeszelContainersCard({ serviceId }: { serviceId: string }) {
  const [rows, setRows] = useState<BeszelContainer[] | null>(null);
  const idRef = useRef(serviceId);
  idRef.current = serviceId;

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const next = await api.beszelContainers(idRef.current);
        if (alive) setRows(next);
      } catch {
        if (alive) setRows([]);
      }
    };
    void load();
    const timer = setInterval(load, POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [serviceId]);

  if (!rows || rows.length === 0) return null;

  return (
    <Card className={css.beszelCard}>
      <div className="card-kicker">Containers · Beszel</div>
      <div className={css.beszelBlocks}>
        {rows.map((c) => (
          <ContainerBlock key={c.name} c={c} />
        ))}
      </div>
    </Card>
  );
}
