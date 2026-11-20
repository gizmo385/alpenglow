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

function rules(u: AutoUpdateUnit): string {
  if (u.follow) return `follows its tag, ${u.every}`;
  const parts = [u.every, `${u.maxBump} only`, `after ${u.soak}`];
  parts.push(u.verdict === "any" ? "any verdict" : `verdict ${u.verdict}${u.verdict === "Needs care" ? " or Easy" : ""}`);
  return parts.join(" · ");
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
          {data.mode === "on" && (data.commit ? " · commits on the server" : " · left uncommitted")}
        </span>
        <Button
          variant="ghost"
          disabled={data.mode === "off" || data.running || busy !== null}
          onClick={() => act("run", () => api.runAutoUpdates(), "Checking automatic updates…")}
        >
          {data.running ? (
            <>
              <Icon name="spinner" size={13} className={styles.spin} /> Running…
            </>
          ) : (
            "Check now"
          )}
        </Button>
      </div>
      {data.errors.map((e) => (
        <div key={e} className={styles.updateError}>
          <Icon name="warning" size={13} /> versions.yaml: {e}
        </div>
      ))}
      {data.units.length > 0 && (
        <Card className={styles.tableCard}>
          <table className="table">
            <thead>
              <tr>
                <th>Service</th>
                <th>Rules</th>
                <th>Next check</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {data.units.map((u) => (
                <tr key={u.id}>
                  <td>
                    <div className={styles.svcName}>{u.services.join(", ") || u.label}</div>
                    <div className={`${styles.mono} ${styles.image}`}>{u.images.join(", ")}</div>
                  </td>
                  <td className={styles.released}>
                    {rules(u)}
                    {u.monitors.length > 0 && <div className={styles.verdictNote}>Kuma: {u.monitors.join(", ")}</div>}
                  </td>
                  <td className={styles.released}>{u.paused ? "paused" : inFuture(u.nextRun)}</td>
                  <td>
                    {u.paused ? (
                      <>
                        <div className={styles.updateError} title={u.paused.reason}>
                          <Icon name="warning" size={13} /> Paused: {u.paused.reason}
                        </div>
                        <Button
                          variant="ghost"
                          disabled={busy !== null}
                          onClick={() =>
                            act(u.id, () => api.resumeAutoUpdate(u.id), `Resumed automatic updates for ${u.label}`)
                          }
                        >
                          Resume
                        </Button>
                      </>
                    ) : (
                      <>
                        {u.lastResult && (
                          <div className={u.lastResult.ok ? styles.autoOk : styles.updateError}>
                            {u.lastResult.ok ? "Updated" : "Failed"} {relativeAgo(u.lastResult.at)}: {u.lastResult.text}
                          </div>
                        )}
                        {u.waiting && <div className={styles.verdictNote}>{u.waiting}</div>}
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </section>
  );
}
