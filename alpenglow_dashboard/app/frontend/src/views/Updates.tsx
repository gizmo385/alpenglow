/* Updates view (C3) — the operator's update queue, fed by image-updates-tracker.
 *
 * Renders the §View 2 table inside a card: ☐ · Service · Image · Current ·
 * Latest · Released · Assessment · Action (changelog link + Update). Busy state
 * for each row is NOT faked locally — it is read from the polled /api/services
 * status (useData().services): while a service's status is "updating" the row's
 * button shows a spinner + "Updating…" and is disabled. Each row's result comes
 * from /api/updates: `updated` shows "Updated" until the tracker's rescan clears
 * the row, and `updateError` says why its last Update failed.
 *
 * One row per pending image. A service with several (signoz: signoz/signoz and
 * its collector) has several rows sharing its id, and they select, update and
 * show busy together, because Update works on the whole service.
 *
 * Update (one row, or "Update selected" for the ticked rows) POSTs
 * /api/updates/apply: the backend repins every image listed in versions.yaml to
 * its new release, one at a time, then pulls & recreates each. The repin is left
 * uncommitted in the server's checkout. It toasts, then nudges the services
 * poll so the busy state appears/clears promptly. The backend rescans once, when
 * the last Update in flight has finished, however many were started.
 *
 * Copy commands use the exact repo path format:
 *   cd /services/{id} && sudo docker compose pull && sudo docker compose up -d
 */

import { useEffect, useRef, useState } from "react";

import { api, ApiError } from "../api/client";
import { useUpdates } from "../api/hooks";
import type { UpdateEntry } from "../api/types";
import { Button, Card } from "../components";
import { Icon } from "../lib/icons";
import { relativeAgo } from "../lib/format";
import { useData } from "../store/data";
import { useToast } from "../store/toast";
import viewStyles from "./views.module.css";
import styles from "./updates/updates.module.css";

/** Exact per-service update command (README §Interactions, PLAN.md C3). */
function updateCommand(id: string): string {
  return `cd /services/${id} && sudo docker compose pull && sudo docker compose up -d`;
}

/** How often to re-check the tracker while an async refresh scan is running. */
const REFRESH_POLL_MS = 1_500;
/** Stop waiting on a scan after this long (it normally settles in well under 15s). */
const REFRESH_TIMEOUT_MS = 90_000;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** Service ids of these rows, once each, in order. */
function uniqueIds(entries: UpdateEntry[]): string[] {
  return [...new Set(entries.map((e) => e.id))];
}

/** Claude's verdict for an update (from upgrades-board), linking to its card. */
function Verdict({ entry }: { entry: UpdateEntry }) {
  if (!entry.verdict) return <span className={styles.noVerdict}>—</span>;
  const label = (
    <>
      <span
        className={styles.verdictDot}
        style={{ background: entry.verdictColor ?? "var(--color-neutral-500)" }}
        aria-hidden
      />
      {entry.verdict}
    </>
  );
  return (
    <div title={entry.verdictNote ?? undefined}>
      {entry.cardUrl ? (
        <a className={styles.verdict} href={entry.cardUrl} target="_blank" rel="noopener noreferrer">
          {label}
        </a>
      ) : (
        <span className={styles.verdict}>{label}</span>
      )}
      {entry.verdictNote && <div className={styles.verdictNote}>{entry.verdictNote}</div>}
    </div>
  );
}

export function Updates() {
  const { data, refresh } = useUpdates();
  const { services, refreshServices } = useData();
  const toast = useToast();
  const [checking, setChecking] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());

  // Guards the refresh poll below: it outlives a quick navigation away, and
  // must not setState (or toast) once this view is gone.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const trackerUnavailable = data?.trackerUnavailable ?? false;
  const entries = trackerUnavailable ? [] : (data?.services ?? []);
  const pending = entries.filter((e) => !e.updated);
  const serviceIds = uniqueIds(pending);
  const count = serviceIds.length;

  // Busy state comes from the polled /api/services data, not local timers.
  const statusById = new Map(services.map((s) => [s.id, s.status]));
  const isBusy = (id: string) => statusById.get(id) === "updating";
  const anyBusy = entries.some((e) => isBusy(e.id));
  const awaitingRescan = entries.some((e) => e.updated);

  // Selection only ever holds rows that are listed, idle and not yet updated.
  const selectable = pending.filter((e) => !isBusy(e.id));
  const chosen = selectable.filter((e) => selected.has(e.id));
  const allChosen = selectable.length > 0 && chosen.length === selectable.length;
  const chosenCount = uniqueIds(chosen).length;

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleAll() {
    setSelected(allChosen ? new Set() : new Set(selectable.map((e) => e.id)));
  }

  // While any row is updating or awaiting the rescan, poll both faster so the
  // button flips promptly (mock settles in ~2s; the base polls are too coarse to
  // see it). Cleared automatically once nothing is left to wait for.
  useEffect(() => {
    if (!anyBusy && !awaitingRescan) return;
    const timer = setInterval(() => {
      refreshServices();
      refresh();
    }, 1500);
    return () => clearInterval(timer);
  }, [anyBusy, awaitingRescan, refreshServices, refresh]);

  // A job's result lands as its busy state clears: read it right away, as the
  // poll above may stop here if the job failed.
  const wasBusy = useRef(false);
  useEffect(() => {
    if (wasBusy.current && !anyBusy) refresh();
    wasBusy.current = anyBusy;
  }, [anyBusy, refresh]);

  /** Ask the tracker to re-scan every image, then wait for the result. */
  async function checkForUpdates() {
    if (checking) return;
    setChecking(true);
    try {
      const started = await api.refreshUpdates();
      if (started.trackerUnavailable) {
        toast("Update tracker is unreachable — can't check right now");
        return;
      }
      // The tracker's /refresh is asynchronous: it returns as soon as the scan
      // *starts*, then queries GitHub for ~8s. Poll until it reports done, so
      // the table lands on fresh data instead of the pre-refresh snapshot.
      const deadline = Date.now() + REFRESH_TIMEOUT_MS;
      let settled = false;
      while (alive.current && Date.now() < deadline) {
        await sleep(REFRESH_POLL_MS);
        if (!alive.current) return;
        try {
          if (!(await api.updates()).refreshing) {
            settled = true;
            break;
          }
        } catch {
          /* transient blip mid-scan — keep waiting until the deadline */
        }
      }
      if (!alive.current) return;
      refresh();
      toast(
        settled
          ? "Update check complete"
          : "Update check is still running — results will appear when it finishes",
      );
    } catch (e) {
      if (!alive.current) return;
      const why =
        e instanceof ApiError && e.status === 403
          ? `you aren't in the admin group`
          : e instanceof Error
            ? e.message
            : String(e);
      toast(`Could not check for updates: ${why}`);
    } finally {
      if (alive.current) setChecking(false);
    }
  }

  /** Repin (when pinned) and pull & recreate these services. */
  async function update(targets: UpdateEntry[], confirm = false): Promise<void> {
    if (targets.length === 0) return;
    const ids = uniqueIds(targets);
    const label = ids.length === 1 ? targets[0].name : `${ids.length} services`;
    // Every pending image of the service is updated, not just this row's.
    const rows = entries.filter((e) => ids.includes(e.id));
    try {
      const res = await api.applyUpdates({ services: ids, confirm });
      if (res.jobs.length > 0) {
        toast(
          res.jobs.length === 1 && rows.length === 1
            ? `Updating ${rows[0].name} to ${rows[0].latestVersion}…`
            : res.jobs.length === 1
              ? `Updating ${label}…`
              : `Updating ${res.jobs.length} services…`,
        );
      }
      for (const r of res.rejected) {
        const name = targets.find((e) => e.id === r.service)?.name ?? r.service;
        toast(`Could not update ${name}: ${r.reason}`);
      }
      setSelected((prev) => {
        const next = new Set(prev);
        for (const j of res.jobs) next.delete(j.service);
        return next;
      });
      // Nudge both polls so the busy overlay and the cleared update appear.
      refreshServices();
      refresh();
    } catch (e) {
      // Guardrail: 409 means confirm required (caddy/postgres/dashboard itself).
      if (e instanceof ApiError && e.status === 409 && !confirm) {
        const ok = window.confirm(
          `${e.message}\n\nUpdating these is guarded and may interrupt service. Update anyway?`,
        );
        if (ok) return update(targets, true);
        return;
      }
      toast(`Could not update ${label}: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  async function copyAll() {
    const text = serviceIds.map(updateCommand).join("\n");
    try {
      await navigator.clipboard.writeText(text);
      toast(`Copied ${count} update command${count === 1 ? "" : "s"}`);
    } catch {
      toast("Could not copy to clipboard");
    }
  }

  async function copyOne(entry: UpdateEntry) {
    try {
      await navigator.clipboard.writeText(updateCommand(entry.id));
      toast(`Copied update command for ${entry.name}`);
    } catch {
      toast("Could not copy to clipboard");
    }
  }

  const line = trackerUnavailable
    ? "Update tracking is currently unavailable"
    : `${count} service${count === 1 ? "" : "s"} have a newer image available`;

  return (
    <div className={`${viewStyles.page} ${viewStyles.narrow}`}>
      <div className={viewStyles.pageHead}>
        <div>
          <h2>Updates</h2>
          <p className={viewStyles.pageSub}>{line}</p>
        </div>
        <div className={styles.headActions}>
          {/* Always offered — including when the tracker is down or everything
              is current, which is exactly when you want to re-check. */}
          <Button variant="secondary" onClick={checkForUpdates} disabled={checking}>
            {checking ? (
              <>
                <Icon name="spinner" size={15} className={styles.spin} /> Checking…
              </>
            ) : (
              <>
                <Icon name="arrow-clockwise" size={15} /> Check for updates
              </>
            )}
          </Button>
          {count > 0 && (
            <>
              <Button variant="secondary" onClick={copyAll}>
                <Icon name="copy" size={15} /> Copy all commands
              </Button>
              <Button
                variant="primary"
                onClick={() => update(chosen)}
                disabled={chosen.length === 0}
                title="Repin and pull & recreate the ticked services"
              >
                <Icon name="arrows-clockwise" size={15} /> Update selected
                {chosenCount > 0 && ` (${chosenCount})`}
              </Button>
            </>
          )}
        </div>
      </div>

      <p className={styles.trackedBy}>
        <Icon name="info" size={14} /> Tracked by image-updates-tracker · cross-checked against
        GitHub releases
        {data?.lastUpdated && ` · last checked ${relativeAgo(data.lastUpdated)}`}
      </p>
      {count > 0 && (
        <p className={styles.trackedBy}>
          <Icon name="push-pin" size={14} /> Images in versions.yaml are repinned to the new
          release before the pull. The change is left uncommitted on the server.
        </p>
      )}

      {trackerUnavailable ? (
        <Card className={styles.fillCard}>
          <Icon name="warning" size={38} weight="fill" color="var(--color-neutral-500)" />
          <div className={styles.fillTitle}>Update tracker unavailable</div>
          <p className={styles.fillSub}>
            image-updates-tracker is unreachable — pending updates can't be listed right now.
          </p>
        </Card>
      ) : entries.length === 0 ? (
        <Card className={styles.fillCard}>
          <Icon name="check-circle" size={38} weight="fill" color="var(--color-ok)" />
          <div className={styles.fillTitle}>Everything is up to date</div>
          <p className={styles.fillSub}>All running images match their latest release.</p>
        </Card>
      ) : (
        <Card className={styles.tableCard}>
          <table className="table">
            <thead>
              <tr>
                <th className={styles.selectCol}>
                  <input
                    type="checkbox"
                    checked={allChosen}
                    disabled={selectable.length === 0}
                    onChange={toggleAll}
                    aria-label="Select all"
                  />
                </th>
                <th>Service</th>
                <th>Image</th>
                <th>Current</th>
                <th>Latest</th>
                <th>Released</th>
                <th>Assessment</th>
                <th className={styles.actionCol}>Action</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => {
                const busy = isBusy(e.id);
                return (
                  <tr key={`${e.id} ${e.image}`}>
                    <td className={styles.selectCol}>
                      <input
                        type="checkbox"
                        checked={!busy && !e.updated && selected.has(e.id)}
                        disabled={busy || e.updated}
                        onChange={() => toggle(e.id)}
                        aria-label={`Select ${e.name}`}
                      />
                    </td>
                    <td>
                      <div className={styles.svcCell}>
                        <Icon name={e.icon} size={15} className={styles.svcIcon} />
                        <span className={styles.svcName}>{e.name}</span>
                      </div>
                    </td>
                    <td className={`${styles.mono} ${styles.image}`}>{e.image}</td>
                    <td className={styles.mono}>{e.currentVersion}</td>
                    <td className={`${styles.mono} ${styles.latest}`}>{e.latestVersion}</td>
                    <td className={styles.released}>{relativeAgo(e.releasedAt)}</td>
                    <td className={styles.verdictCell}>
                      <Verdict entry={e} />
                    </td>
                    <td>
                      <div className={styles.actionCell}>
                        {e.changelogUrl && (
                          <a
                            className={`btn btn-ghost ${styles.changelog}`}
                            href={e.changelogUrl}
                            target="_blank"
                            rel="noopener noreferrer"
                            title="Changelog"
                            aria-label={`Changelog for ${e.name}`}
                          >
                            <Icon name="note" size={15} />
                          </a>
                        )}
                        <Button
                          variant="primary"
                          className={styles.pullBtn}
                          disabled={busy || e.updated}
                          onClick={() => update([e])}
                          onContextMenu={(ev) => {
                            ev.preventDefault();
                            void copyOne(e);
                          }}
                          title={
                            busy || e.updated
                              ? undefined
                              : "Repin and pull & recreate (right-click to copy command)"
                          }
                        >
                          {busy ? (
                            <>
                              <Icon name="spinner" size={13} className={styles.spin} /> Updating…
                            </>
                          ) : e.updated ? (
                            <>
                              <Icon name="check-circle" size={13} /> Updated
                            </>
                          ) : (
                            <>
                              <Icon name="arrows-clockwise" size={13} /> Update
                            </>
                          )}
                        </Button>
                      </div>
                      {!busy && e.updateError && (
                        <div className={styles.updateError} title={e.updateError}>
                          <Icon name="warning" size={13} /> Update failed: {e.updateError}
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}
