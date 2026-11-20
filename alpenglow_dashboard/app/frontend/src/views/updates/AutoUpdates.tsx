/* Automatic updates (backend autoupdate/).
 *
 * Two places in the Updates view:
 *
 * - <AutoNote> under a pending row's service name says when that update will
 *   go out by itself ("Auto · in 19h"), or just "Auto" while it waits for
 *   Claude's assessment (shown in the Assessment column). Rows that
 *   stay manual (too big, or flagged) get no note.
 * - <AutoUpdates> below the table lists only what needs a look: units paused
 *   after a failed update (with Resume) and what updated automatically in the
 *   last week. The opted-in services and their rules are behind "Show rules".
 *
 * Hidden when nothing opts in and versions.yaml has no auto problems.
 */

import { useState } from "react";

import { api, ApiError } from "../../api/client";
import type { AutoUpdates as AutoUpdatesData, AutoUpdateUnit, UpdateEntry } from "../../api/types";
import { Button, Card } from "../../components";
import { Icon } from "../../lib/icons";
import { relativeAgo } from "../../lib/format";
import { useToast } from "../../store/toast";
import styles from "./updates.module.css";

const RECENT_MS = 7 * 24 * 3600 * 1000;

const cap = (t: string) => t.charAt(0).toUpperCase() + t.slice(1);

/** "in 19h", or "Fri 4:00 AM" once it's more than a day away. */
function when(iso: string): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "soon";
  const mins = Math.round((then - Date.now()) / 60_000);
  if (mins <= 1) return "shortly";
  if (mins < 60) return `in ${mins}m`;
  if (mins < 24 * 60) return `in ${Math.round(mins / 60)}h`;
  return new Date(then).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
}

/** The versions.yaml key for an image ref: no tag, digest or docker.io prefix. */
function imageKey(ref: string): string {
  let name = ref.split("@")[0];
  const slash = name.lastIndexOf("/");
  const colon = name.lastIndexOf(":");
  if (colon > slash) name = name.slice(0, colon);
  for (const prefix of ["docker.io/", "index.docker.io/"]) {
    if (name.startsWith(prefix)) name = name.slice(prefix.length);
  }
  return name.startsWith("library/") ? name.slice("library/".length) : name;
}

export function unitFor(data: AutoUpdatesData | null, entry: UpdateEntry): AutoUpdateUnit | undefined {
  if (!data) return undefined;
  const key = imageKey(entry.image);
  return data.units.find((u) => u.images.includes(key));
}

/** One line under a pending update's service name. */
export function AutoNote({ unit, mode }: { unit: AutoUpdateUnit | undefined; mode: AutoUpdatesData["mode"] }) {
  if (!unit || mode === "off" || unit.follow) return null;
  const h = unit.hold;
  let text: string;
  let tone = styles.autoNote;
  if (unit.paused || h?.kind === "paused") {
    text = "Auto paused";
    tone = `${styles.autoNote} ${styles.autoNoteBad}`;
  } else {
    const needs = h?.kind === "assessment" || (h?.kind === "soak" && unit.then === "assessment");
    const blocked = h && !needs && !(h.kind === "soak" && !unit.then);
    if (blocked || !unit.eta) return null; // too big or flagged: a normal manual update
    // Waiting on Claude: the Assessment column beside it already says so.
    text = needs && unit.verdict !== "any" ? "Auto" : `Auto · ${when(unit.eta)}`;
    if (mode === "dry-run") text += " (dry run)";
  }
  return (
    <div className={tone} title={h ? cap(h.text) : "Goes out at its next scheduled check"}>
      <Icon name="arrows-clockwise" size={11} /> {text}
    </div>
  );
}

const VERDICT_COLOR: Record<string, string> = { Easy: "#2e7d32", "Needs care": "#f9a825" };

/** "daily" → "Daily", "6h" → "Every 6h", "0 4 * * *" → "Daily 4:00". */
function schedule(every: string): string {
  const daily = /^(\d{1,2}) (\d{1,2}) \* \* \*$/.exec(every);
  if (daily) return `Daily ${daily[2]}:${daily[1].padStart(2, "0")}`;
  if (every.split(" ").length === 5) return every;
  return /^\d/.test(every) ? `Every ${every}` : cap(every);
}

function short(span: string | null): string {
  const m = span && /^(\d+) (\w)/.exec(span);
  if (m && m[1] === "0") return "None";
  return m ? `${m[1]}${m[2]}` : "—";
}

function RulesTable({ units }: { units: AutoUpdateUnit[] }) {
  return (
    <Card className={styles.tableCard}>
      <table className={`table ${styles.autoTable}`}>
        <thead>
          <tr>
            <th>Service</th>
            <th>Checks</th>
            <th title="Largest update that goes out by itself">Up to</th>
            <th title="How long a release must be out first">Soak</th>
            <th title="Claude's assessment it needs">Verdict</th>
            <th>Health check</th>
          </tr>
        </thead>
        <tbody>
          {units.map((u) => (
            <tr key={u.id}>
              <td>
                <div className={styles.svcName}>{u.services.join(", ") || u.label}</div>
                <div className={`${styles.mono} ${styles.image}`}>{u.images.join(", ")}</div>
              </td>
              <td className={styles.released}>{schedule(u.every)}</td>
              <td className={styles.released}>{u.follow ? "New image on its tag" : cap(u.maxBump ?? "")}</td>
              <td className={styles.released}>{u.follow ? "—" : short(u.soak)}</td>
              <td className={styles.verdictCell}>
                {u.follow || !u.verdict ? (
                  <span className={styles.noVerdict}>—</span>
                ) : u.verdict === "any" ? (
                  <span className={styles.released}>Any</span>
                ) : (
                  <span className={styles.verdict}>
                    <span className={styles.verdictDot} style={{ background: VERDICT_COLOR[u.verdict] }} aria-hidden />
                    {u.verdict === "Needs care" ? "Needs care or better" : "Easy"}
                  </span>
                )}
              </td>
              <td className={styles.released}>
                {u.monitors.length ? `Containers + ${u.monitors.join(", ")}` : "Containers"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

export function AutoUpdates({ data, refresh }: { data: AutoUpdatesData | null; refresh: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const [showRules, setShowRules] = useState(false);

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

  const paused = data.units.filter((u) => u.paused);
  const recent = data.units.filter(
    (u) => !u.paused && u.lastResult && Date.now() - Date.parse(u.lastResult.at) < RECENT_MS,
  );
  const name = (u: AutoUpdateUnit) => u.services.join(", ") || u.label;
  const mode =
    data.mode === "off"
      ? "Off"
      : data.mode === "dry-run"
        ? "Dry run: only announced on Discord"
        : data.commit
          ? "On · committed on the server"
          : "On · repins left uncommitted";

  return (
    <section className={styles.autoSection}>
      <div className={styles.autoHead}>
        <h2 className={styles.autoTitle}>Automatic updates</h2>
        <span className={styles.autoMode}>
          {mode} · {data.units.length} opted in
        </span>
        {data.units.length > 0 && (
          <Button variant="ghost" onClick={() => setShowRules((v) => !v)}>
            {showRules ? "Hide rules" : "Show rules"}
          </Button>
        )}
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

      {paused.length + recent.length > 0 ? (
        <Card className={styles.autoList}>
          {paused.map((u) => (
            <div key={u.id} className={styles.autoItem}>
              <span className={styles.verdictDot} style={{ background: "var(--color-down)" }} aria-hidden />
              <div className={styles.autoItemText}>
                <span className={styles.svcName}>{name(u)}</span> paused after a failed automatic update
                <div className={styles.verdictNote} title={u.paused!.reason}>
                  {u.paused!.reason} · not rolled back · {relativeAgo(u.paused!.at)}
                </div>
              </div>
              <Button
                variant="primary"
                className={styles.pullBtn}
                disabled={busy !== null}
                title="Let automatic updates run for this again"
                onClick={() => act(u.id, () => api.resumeAutoUpdate(u.id), `Resumed automatic updates for ${name(u)}`)}
              >
                <Icon name="play" size={13} /> Resume
              </Button>
            </div>
          ))}
          {recent.map((u) => (
            <div key={u.id} className={styles.autoItem}>
              <span
                className={styles.verdictDot}
                style={{ background: u.lastResult!.ok ? "var(--color-ok)" : "var(--color-down)" }}
                aria-hidden
              />
              <div className={styles.autoItemText}>
                <span className={styles.svcName}>{name(u)}</span>{" "}
                {u.lastResult!.ok ? "updated automatically" : "automatic update failed"}
                <div className={styles.verdictNote}>
                  {u.lastResult!.text} · {relativeAgo(u.lastResult!.at)}
                </div>
              </div>
            </div>
          ))}
        </Card>
      ) : (
        !showRules && (
          <p className={styles.trackedBy}>
            Nothing has updated automatically this week. Pending updates that will go out by themselves are marked
            in the table above.
          </p>
        )
      )}

      {showRules && <RulesTable units={data.units} />}
    </section>
  );
}
