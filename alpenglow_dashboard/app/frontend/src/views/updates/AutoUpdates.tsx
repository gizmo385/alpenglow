/* Automatic updates — the units versions.yaml opts in (backend autoupdate/).
 *
 * One row per unit (an image, a group, or a followed image): its schedule and
 * rules, when it's next checked, and why nothing went out last time. A unit
 * paused after a failed update shows the reason and a Resume button; "Check
 * now" runs every unit straight away, ignoring the schedules. Hidden entirely
 * when nothing opts in and versions.yaml has no auto problems.
 */

import { useState } from "react";

import { api, ApiError } from "../../api/client";
import { useAutoUpdates } from "../../api/hooks";
import type { AutoUpdateUnit } from "../../api/types";
import { Button, Card } from "../../components";
import { Icon } from "../../lib/icons";
import { relativeAgo } from "../../lib/format";
import { useToast } from "../../store/toast";
import styles from "./updates.module.css";

const MODE_LABEL = {
  off: "Off",
  "dry-run": "Dry run: announces on Discord, changes nothing",
  on: "On",
} as const;

function inFuture(iso: string | null): string {
  if (!iso) return "after its first interval";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  const mins = Math.round((then - Date.now()) / 60_000);
  if (mins <= 0) return "due now";
  if (mins < 60) return `in ${mins}m`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `in ${hours}h`;
  return `in ${Math.round(hours / 24)} days`;
}

const cap = (t: string) => t.charAt(0).toUpperCase() + t.slice(1);

/** "daily" → "Daily", "6h" → "Every 6h", "0 4 * * *" → "Daily 4:00". */
function schedule(every: string): string {
  const daily = /^(\d{1,2}) (\d{1,2}) \* \* \*$/.exec(every);
  if (daily) return `Daily ${daily[2]}:${daily[1].padStart(2, "0")}`;
  if (every.split(" ").length === 5) return every;
  return /^\d/.test(every) ? `Every ${every}` : cap(every);
}

/** "2 days" → "2d", "12 hours" → "12h". */
function short(span: string | null): string {
  const m = span && /^(\d+) (\w)/.exec(span);
  return m ? `${m[1]}${m[2]}` : "—";
}

const VERDICT_COLOR: Record<string, string> = {
  Easy: "#2e7d32",
  "Needs care": "#f9a825",
};

function Verdict({ u }: { u: AutoUpdateUnit }) {
  if (u.follow || !u.verdict) return <span className={styles.noVerdict}>—</span>;
  if (u.verdict === "any") return <span>Any</span>;
  return (
    <span className={styles.verdict} title={u.verdict === "Needs care" ? "Easy or Needs care" : "Easy only"}>
      <span className={styles.verdictDot} style={{ background: VERDICT_COLOR[u.verdict] }} aria-hidden />
      {u.verdict === "Needs care" ? "Needs care" : "Easy"}
    </span>
  );
}

type Status = { label: string; color: string; note: string | null };

function status(u: AutoUpdateUnit): Status {
  if (u.paused) return { label: "Paused", color: "var(--color-down)", note: u.paused.reason };
  const r = u.lastResult;
  const waiting = u.waiting && u.waiting !== "up to date" ? u.waiting : null;
  if (waiting) return { label: "Waiting", color: "var(--color-neutral-500)", note: waiting };
  if (r) {
    return r.ok
      ? { label: `Updated ${relativeAgo(r.at)}`, color: "var(--color-ok)", note: r.text }
      : { label: `Failed ${relativeAgo(r.at)}`, color: "var(--color-down)", note: r.text };
  }
  return { label: "Up to date", color: "var(--color-ok)", note: null };
}

export function AutoUpdates() {
  const { data, refresh } = useAutoUpdates();
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);

  if (!data || (data.units.length === 0 && data.errors.length === 0)) return null;

  const act = async (key: string, call: () => Promise<unknown>, done: string) => {
    setBusy(key);
    try {
      await call();
      toast(done);
      refresh();
    } catch (e) {
      toast(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className={styles.autoSection}>
      <div className={styles.autoHead}>
        <h2 className={styles.autoTitle}>Automatic updates</h2>
        <span className={styles.autoMode}>
          {MODE_LABEL[data.mode]}
          {data.mode === "on" && (data.commit ? " · committed on the server" : " · repins left uncommitted")}
        </span>
        <Button
          variant="secondary"
          disabled={data.mode === "off" || data.running || busy !== null}
          onClick={() => act("run", () => api.runAutoUpdates(), "Checking automatic updates…")}
        >
          {data.running ? (
            <>
              <Icon name="spinner" size={15} className={styles.spin} /> Checking…
            </>
          ) : (
            <>
              <Icon name="arrow-clockwise" size={15} /> Check now
            </>
          )}
        </Button>
      </div>
      {data.errors.map((e) => (
        <p key={e} className={styles.autoError}>
          <Icon name="warning" size={13} /> versions.yaml: {e}
        </p>
      ))}
      {data.units.length > 0 && (
        <Card className={styles.tableCard}>
          <table className={`table ${styles.autoTable}`}>
            <thead>
              <tr>
                <th>Service</th>
                <th>Schedule</th>
                <th title="Largest update that goes out by itself">Up to</th>
                <th title="How long a release must be out first">Soak</th>
                <th title="Claude's assessment it needs">Verdict</th>
                <th>Next check</th>
                <th>Status</th>
                <th className={styles.actionCol} aria-label="Action" />
              </tr>
            </thead>
            <tbody>
              {data.units.map((u) => {
                const st = status(u);
                return (
                  <tr key={u.id}>
                    <td>
                      <div className={styles.svcName}>{u.services.join(", ") || u.label}</div>
                      <div className={`${styles.mono} ${styles.image}`}>{u.images.join(", ")}</div>
                      {u.monitors.length > 0 && (
                        <div className={styles.autoMonitor} title="Uptime Kuma monitors checked after each update">
                          <Icon name="heartbeat" size={12} /> {u.monitors.join(", ")}
                        </div>
                      )}
                    </td>
                    <td className={styles.released}>{schedule(u.every)}</td>
                    <td className={styles.released}>{u.follow ? "New tag" : cap(u.maxBump ?? "")}</td>
                    <td className={styles.released}>{u.follow ? "—" : short(u.soak)}</td>
                    <td className={styles.verdictCell}>
                      <Verdict u={u} />
                    </td>
                    <td className={styles.released}>{u.paused ? "—" : cap(inFuture(u.nextRun))}</td>
                    <td className={styles.verdictCell}>
                      <span className={styles.verdict}>
                        <span className={styles.verdictDot} style={{ background: st.color }} aria-hidden />
                        {st.label}
                      </span>
                      {st.note && (
                        <div className={styles.verdictNote} title={st.note}>
                          {st.note}
                        </div>
                      )}
                    </td>
                    <td>
                      <div className={styles.actionCell}>
                        {u.paused && (
                          <Button
                            variant="primary"
                            className={styles.pullBtn}
                            disabled={busy !== null}
                            title="Let automatic updates run for this again"
                            onClick={() =>
                              act(u.id, () => api.resumeAutoUpdate(u.id), `Resumed automatic updates for ${u.label}`)
                            }
                          >
                            <Icon name="play" size={13} /> Resume
                          </Button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Card>
      )}
    </section>
  );
}
