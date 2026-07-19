/* Resources tab (handoff §View 3 → Resources tab).
 *
 * Two cards (CPU % big number + accent-400 sparkline; Memory used/limit + #6bd39a
 * sparkline) built from GET /stats history, plus a row card with Net I/O, Block
 * I/O, PIDs, Restarts. Stats poll every 15s while this tab is mounted (the tab
 * only mounts while active, so the poll pauses when you leave). */

import { useEffect, useRef, useState } from "react";

import { api } from "../../api/client";
import type { Stats } from "../../api/types";
import { Card, Sparkline } from "../../components";
import { formatBytes } from "../../lib/format";
import { BeszelContainersCard } from "./BeszelContainersCard";
import css from "./detail.module.css";

const STATS_POLL_MS = 15_000;

function memLabel(bytes: number | null | undefined): string {
  if (bytes == null) return "—";
  return formatBytes(bytes);
}

export function ResourcesTab({ serviceId }: { serviceId: string }) {
  const [stats, setStats] = useState<Stats | null>(null);
  const [error, setError] = useState(false);
  // Keep serviceId fresh without retriggering the interval effect each render.
  const idRef = useRef(serviceId);
  idRef.current = serviceId;

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const next = await api.stats(idRef.current);
        if (alive) {
          setStats(next);
          setError(false);
        }
      } catch {
        if (alive) setError(true);
      }
    };
    void load();
    const timer = setInterval(load, STATS_POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [serviceId]);

  if (error && !stats) {
    return <div className={css.loadingNote}>Stats unavailable.</div>;
  }
  if (!stats) {
    return <div className={css.loadingNote}>Loading resource stats…</div>;
  }

  const { history, current } = stats;
  const cpuValues = history.cpu.map((p) => p.v);
  const memValues = history.mem.map((p) => p.v);
  const cpuPct = current.cpuPct != null ? Math.round(current.cpuPct) : "—";
  const memUsed = memLabel(current.memUsed);
  const memLimit = current.memLimit != null ? formatBytes(current.memLimit) : "—";

  return (
    <>
      <div className={css.resGrid}>
        <Card>
          <div className="card-kicker">CPU</div>
          <div className={css.bigNum}>
            {cpuPct}
            <span className={css.bigNumUnit}>%</span>
          </div>
          <Sparkline values={cpuValues} stroke="var(--color-accent-400)" width={240} height={60} />
        </Card>
        <Card>
          <div className="card-kicker">Memory</div>
          <div className={css.bigNum}>
            {memUsed}
            <span className={css.bigNumUnit}> / {memLimit}</span>
          </div>
          <Sparkline values={memValues} stroke="#6bd39a" width={240} height={60} />
        </Card>
      </div>

      <Card className={css.resRow}>
        <div>
          <div className="card-kicker">Net I/O</div>
          <div className={css.resStat}>{current.netIO ?? "—"}</div>
        </div>
        <div>
          <div className="card-kicker">Block I/O</div>
          <div className={css.resStat}>{current.blockIO ?? "—"}</div>
        </div>
        <div>
          <div className="card-kicker">PIDs</div>
          <div className={css.resStat}>{current.pids ?? "—"}</div>
        </div>
        <div>
          <div className="card-kicker">Restarts</div>
          <div className={css.resStat}>{current.restarts ?? "—"}</div>
        </div>
      </Card>

      <BeszelContainersCard serviceId={serviceId} />
    </>
  );
}
