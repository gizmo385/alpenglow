/* Updates view (C3) — the operator's update queue, fed by image-updates-tracker.
 *
 * Renders the §View 2 table inside a card: Service · Image · Current · Latest ·
 * Released · Action (changelog link + Pull & recreate). Busy state for each row
 * is NOT faked locally — it is read from the polled /api/services status
 * (useData().services): while a service's status is "updating" the row's button
 * shows a spinner + "Pulling…" and is disabled. Firing an action (Pull &
 * recreate / Update all) POSTs via the api client (which attaches CSRF), toasts,
 * then nudges the services poll so the busy state appears/clears promptly.
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

export function Updates() {
  const { data, refresh } = useUpdates();
  const { services, refreshServices } = useData();
  const toast = useToast();
  const [checking, setChecking] = useState(false);

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
  const count = entries.length;

  // Busy state comes from the polled /api/services data, not local timers.
  const statusById = new Map(services.map((s) => [s.id, s.status]));
  const isBusy = (id: string) => statusById.get(id) === "updating";
  const anyBusy = entries.some((e) => isBusy(e.id));

  // While any row is updating, poll /api/services faster so the button flips
  // to "Pulling…" and back promptly (mock settles in ~2s; the 10s base poll is
  // too coarse to see it). Cleared automatically once nothing is busy.
  useEffect(() => {
    if (!anyBusy) return;
    const timer = setInterval(refreshServices, 1500);
    return () => clearInterval(timer);
  }, [anyBusy, refreshServices]);

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

  async function pull(entry: UpdateEntry, confirm = false) {
    try {
      await api.action(entry.id, { action: "pull", confirm });
      toast(`Pulling ${entry.latestVersion} for ${entry.name}…`);
      // Nudge both polls so the busy overlay and the cleared update appear.
      refreshServices();
      refresh();
    } catch (e) {
      // Guardrail: 409 means confirm required (caddy/postgres/dashboard itself).
      if (e instanceof ApiError && e.status === 409 && !confirm) {
        const ok = window.confirm(
          `Updating ${entry.name} is guarded and may interrupt service. Pull & recreate anyway?`,
        );
        if (ok) return pull(entry, true);
        return;
      }
      toast(`Could not update ${entry.name}: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  async function updateAll() {
    const pending = entries.filter((e) => !isBusy(e.id));
    if (pending.length === 0) return;
    toast(`Updating ${pending.length} service${pending.length === 1 ? "" : "s"}…`);
    await Promise.all(pending.map((e) => pull(e)));
  }

  async function copyAll() {
    const text = entries.map((e) => updateCommand(e.id)).join("\n");
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
              <Button variant="primary" onClick={updateAll}>
                <Icon name="arrows-clockwise" size={15} /> Update all
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

      {trackerUnavailable ? (
        <Card className={styles.fillCard}>
          <Icon name="warning" size={38} weight="fill" color="var(--color-neutral-500)" />
          <div className={styles.fillTitle}>Update tracker unavailable</div>
          <p className={styles.fillSub}>
            image-updates-tracker is unreachable — pending updates can't be listed right now.
          </p>
        </Card>
      ) : count === 0 ? (
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
                <th>Service</th>
                <th>Image</th>
                <th>Current</th>
                <th>Latest</th>
                <th>Released</th>
                <th className={styles.actionCol}>Action</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => {
                const busy = isBusy(e.id);
                return (
                  <tr key={e.id}>
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
                          disabled={busy}
                          onClick={() => pull(e)}
                          onContextMenu={(ev) => {
                            ev.preventDefault();
                            void copyOne(e);
                          }}
                          title={busy ? undefined : "Pull & recreate (right-click to copy command)"}
                        >
                          {busy ? (
                            <>
                              <Icon name="spinner" size={13} className={styles.spin} /> Pulling…
                            </>
                          ) : (
                            <>
                              <Icon name="arrows-clockwise" size={13} /> Pull &amp; recreate
                            </>
                          )}
                        </Button>
                      </div>
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
