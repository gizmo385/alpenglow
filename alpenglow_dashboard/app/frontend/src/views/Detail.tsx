/* Service detail view (C4).
 *
 * Structure:
 *   - Back ghost button "← Overview"
 *   - Header: 46×46 icon tile, name + status tag (colored dot, pulse while busy),
 *     mono image line; right-aligned Open (.btn-secondary, hidden when no url).
 *   - Action bar: Pull & recreate (only when an update is available), Restart,
 *     Stop/Start, spacer, Copy update cmd. Buttons disable + spin while busy.
 *     Actions POST /api/services/{id}/actions; a guardrail 409 → confirm flow
 *     (window.confirm; caddy gets the connection-drop warning) → retry confirm.
 *   - Tabs (underline, accent active): Overview / Resources / Logs / Compose.
 *
 * Data sources:
 *   - The shared services poll (useData) supplies the live status that drives the
 *     busy state (status flips every ~10s poll) so buttons re-enable on settle.
 *   - api.service(id) supplies the full ServiceDetail facts (refetched when the
 *     poll delivers a materially changed summary, and after tag edits).
 *   - Resources/Logs/Compose tabs own their own /stats, /logs (SSE), /compose. */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowClockwiseIcon,
  ArrowFatUpIcon,
  ArrowLeftIcon,
  ArrowSquareOutIcon,
  PlayIcon,
  SpinnerIcon,
  StopIcon,
  TerminalWindowIcon,
} from "@phosphor-icons/react";
import { useNavigate, useParams } from "react-router-dom";

import { api, ApiError } from "../api/client";
import type { ActionName, ServiceDetail } from "../api/types";
import { Button } from "../components";
import { Icon } from "../lib/icons";
import { useData } from "../store/data";
import { useToast } from "../store/toast";
import { ComposeTab } from "./detail/ComposeTab";
import { LogsTab } from "./detail/LogsTab";
import { OverviewTab, STATUS_LABEL, STATUS_VAR } from "./detail/OverviewTab";
import { ResourcesTab } from "./detail/ResourcesTab";
import styles from "./views.module.css";
import head from "./Detail.module.css";
import css from "./detail/detail.module.css";

type Tab = "overview" | "resources" | "logs" | "compose";
const TABS: { key: Tab; label: string }[] = [
  { key: "overview", label: "Overview" },
  { key: "resources", label: "Resources" },
  { key: "logs", label: "Logs" },
  { key: "compose", label: "Compose" },
];

export function Detail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const toast = useToast();
  const { services, refreshServices } = useData();

  // Live summary from the shared 10s poll — drives status/busy + version bumps.
  const summary = useMemo(() => services.find((s) => s.id === id), [services, id]);

  const [detail, setDetail] = useState<ServiceDetail | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [notFound, setNotFound] = useState(false);

  const loadDetail = useCallback(async () => {
    if (!id) return;
    try {
      setDetail(await api.service(id));
      setNotFound(false);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) setNotFound(true);
    }
  }, [id]);

  useEffect(() => {
    void loadDetail();
  }, [loadDetail]);

  // Re-fetch the full detail when the poll reports a status/version change, so
  // the facts grid (Version, Status, Uptime) tracks actions without a manual
  // refresh. Keyed on the fields that actually appear in the facts.
  const summaryKey = summary
    ? `${summary.status}|${summary.currentVersion}|${summary.latestVersion}|${summary.uptimeSeconds}`
    : "";
  useEffect(() => {
    if (summaryKey) void loadDetail();
  }, [summaryKey, loadDetail]);

  // Merge the fetched detail with the live poll. The Overview tab only needs
  // ServiceSummary fields (all present on the summary), so we can render facts
  // immediately from the poll and let the api.service() fetch enrich the
  // detail-only fields (composePath/projectName/primaryContainer) when it lands.
  // The volatile bits (status/uptime/version/tags) always come from the poll.
  const svc: ServiceDetail | null = useMemo(() => {
    const base = detail ?? summary;
    if (!base) return null;
    return {
      // detail-only fields default to summary-derived placeholders until fetched
      composePath: "",
      projectName: base.id,
      primaryContainer: null,
      ...base,
      // live poll wins for the volatile facts
      status: summary?.status ?? base.status,
      uptimeSeconds: summary?.uptimeSeconds ?? base.uptimeSeconds,
      currentVersion: summary?.currentVersion ?? base.currentVersion,
      latestVersion: summary?.latestVersion ?? base.latestVersion,
      tags: summary?.tags ?? base.tags,
    };
  }, [detail, summary]);

  const status = svc?.status ?? summary?.status ?? "up";
  const busy = status === "restarting" || status === "updating";
  const hasUpdate = Boolean(svc?.latestVersion ?? summary?.latestVersion);
  const url = svc?.url ?? summary?.url ?? null;
  const latest = svc?.latestVersion ?? summary?.latestVersion ?? null;
  const name = svc?.name ?? summary?.name ?? id;
  const icon = svc?.icon ?? summary?.icon;
  const image = svc?.image ?? summary?.image;

  const copyCmd = id
    ? `cd /services/${id} && sudo docker compose pull && sudo docker compose up -d`
    : "";

  async function runAction(action: ActionName, confirm = false) {
    if (!id) return;
    try {
      await api.action(id, { action, confirm });
      const verb =
        action === "pull"
          ? "Pulling & recreating"
          : action === "restart"
            ? "Restarting"
            : action === "stop"
              ? "Stopping"
              : "Starting";
      toast(`${verb} ${name}…`);
      // Kick the shared poll so the busy status shows up promptly.
      refreshServices();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && !confirm) {
        // Guardrail: the backend names the guarded service in the detail.
        const names = /caddy/i.test(e.detail);
        const extra = names
          ? "\n\nWarning: this will restart the reverse proxy and drop your own connection to the dashboard."
          : "";
        if (window.confirm(`This is a protected service. Proceed with “${action}”?${extra}`)) {
          void runAction(action, true);
        }
        return;
      }
      toast(`Action failed: ${e instanceof ApiError ? e.detail : String(e)}`);
    }
  }

  async function copyUpdateCmd() {
    try {
      await navigator.clipboard.writeText(copyCmd);
      toast("Update command copied");
    } catch {
      toast("Copy failed");
    }
  }

  if (notFound || (!svc && !summary)) {
    return (
      <div className={`${styles.page} ${styles.detail}`}>
        <Button variant="ghost" className={styles.backBtn} onClick={() => navigate("/")}>
          <ArrowLeftIcon size={16} /> Overview
        </Button>
        <p className={styles.placeholder}>Unknown service “{id}”.</p>
      </div>
    );
  }

  const statusTagClass =
    status === "down" ? "down" : status === "up" ? "up" : "busy";

  return (
    <div className={`${styles.page} ${styles.detail}`}>
      <Button variant="ghost" className={styles.backBtn} onClick={() => navigate("/")}>
        <ArrowLeftIcon size={16} /> Overview
      </Button>

      {/* ── header ── */}
      <div className={head.header}>
        <span className={head.iconTile}>
          <Icon name={icon} size={24} color="var(--color-accent-300)" />
        </span>
        <div className={head.headMain}>
          <div className={css.headTop}>
            <h2>{name}</h2>
            <span className={`tag ${css.statusTag} ${css[statusTagClass]}`}>
              <span
                className={`${css.statusTagDot}${busy ? ` ${css.pulse}` : ""}`}
                style={{ background: STATUS_VAR[status] }}
              />
              {STATUS_LABEL[status]}
            </span>
          </div>
          {image && <div className={head.image}>{image}</div>}
        </div>
        {url && (
          <div className={css.headActions}>
            <a className="btn btn-secondary" href={url} target="_blank" rel="noopener noreferrer">
              <ArrowSquareOutIcon size={14} /> Open
            </a>
          </div>
        )}
      </div>

      {/* ── action bar ── */}
      <div className={css.actionBar}>
        {hasUpdate && (
          <Button variant="primary" onClick={() => runAction("pull")} disabled={busy}>
            {status === "updating" ? (
              <SpinnerIcon size={14} className={css.spin} />
            ) : (
              <ArrowFatUpIcon size={14} />
            )}
            {status === "updating" ? "Pulling…" : `Pull & recreate → ${latest}`}
          </Button>
        )}
        <Button variant="secondary" onClick={() => runAction("restart")} disabled={busy}>
          {status === "restarting" ? (
            <SpinnerIcon size={14} className={css.spin} />
          ) : (
            <ArrowClockwiseIcon size={14} />
          )}
          Restart
        </Button>
        {status === "down" ? (
          <Button variant="secondary" onClick={() => runAction("start")} disabled={busy}>
            <PlayIcon size={14} /> Start
          </Button>
        ) : (
          <Button variant="secondary" onClick={() => runAction("stop")} disabled={busy}>
            <StopIcon size={14} /> Stop
          </Button>
        )}
        <div className={css.actionSpacer} />
        <Button variant="secondary" onClick={copyUpdateCmd}>
          <TerminalWindowIcon size={14} /> Copy update cmd
        </Button>
      </div>

      {/* ── tabs ── */}
      <div className={css.tabs} role="tablist">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={tab === t.key}
            className={`${css.tab}${tab === t.key ? ` ${css.active}` : ""}`}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* ── tab bodies ── */}
      {tab === "overview" &&
        (svc ? (
          <OverviewTab svc={svc} onTagsChanged={refreshServices} />
        ) : (
          <div className={css.loadingNote}>Loading service details…</div>
        ))}
      {tab === "resources" && id && <ResourcesTab serviceId={id} />}
      {tab === "logs" && id && <LogsTab serviceId={id} />}
      {tab === "compose" && id && <ComposeTab serviceId={id} />}
    </div>
  );
}
