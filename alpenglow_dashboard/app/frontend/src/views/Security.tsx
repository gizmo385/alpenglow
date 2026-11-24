/* Security view: every service with open findings, grouped by Claude's
 * verdict, so a service with findings but no pending update is still easy to
 * find (the Updates page only lists services with something to pull).
 *
 * The groups are sorted by what there is to do. Act now and Next upgrade need
 * a step, and each row leads with the reason. Waiting on upstream and Not
 * affected need nothing, so their rows are denser.
 *
 * Data: GET /api/security, polled every 60s. Reassess goes through
 * POST /api/services/{id}/security/reassess (rescans the image first), and
 * "Reassess all" through POST /api/security/reassess. */

import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowClockwiseIcon } from "@phosphor-icons/react";

import { api, ApiError } from "../api/client";
import { useSecurity } from "../api/hooks";
import type { SecurityServiceRow } from "../api/types";
import { Button, Card } from "../components";
import { relativeAgo } from "../lib/format";
import { Icon } from "../lib/icons";
import { useToast } from "../store/toast";
import { ShieldLabel } from "./security/Shield";
import css from "./security/security.module.css";
import views from "./views.module.css";

const WAITING = "Waiting for assessment";

/** Display order, with what each verdict means for the person reading. */
const GROUPS: { verdict: string; hint: string; quiet: boolean }[] = [
  { verdict: "Act now", hint: "exploitable as we run it", quiet: false },
  { verdict: "Next upgrade", hint: "a fix you can take today", quiet: false },
  { verdict: WAITING, hint: "Claude is looking at these", quiet: true },
  { verdict: "Waiting on upstream", hint: "nothing to do yet", quiet: true },
  { verdict: "Not affected", hint: "not reachable as we run it", quiet: true },
];

/** Exposure filter: management is tailnet-only too, so it's counted there. */
const EXPOSURES: { key: string; label: string; tiers: string[] }[] = [
  { key: "public", label: "Public", tiers: ["public"] },
  { key: "tailnet", label: "Tailnet", tiers: ["tailnet", "management"] },
  { key: "internal", label: "No ingress", tiers: ["internal"] },
];

function exposureOf(row: SecurityServiceRow) {
  return EXPOSURES.find((e) => e.tiers.includes(row.tier ?? "internal")) ?? EXPOSURES[2];
}

/** "1.30.4 · latest release", "1.20.27 · 1.20.28 is out", "2.10.2 · built on the server". */
function Version({ row }: { row: SecurityServiceRow }) {
  const running = row.version ?? "version unknown";
  if (row.latestVersion) {
    return (
      <span>
        {running} · <b className={css.newer}>{row.latestVersion} is out</b>
      </span>
    );
  }
  if (row.localBuild) return <span>{running} · built on the server</span>;
  return <span>{running} · {row.tracked ? "latest release" : "not tracked"}</span>;
}

function Reviewed({ row }: { row: SecurityServiceRow }) {
  if (row.rescanning) return <span className={css.pending}>rescanning the image…</span>;
  if (row.verdict === WAITING) return <span className={css.pending}>being assessed…</span>;
  return <span>{row.verdictAt ? `reviewed ${relativeAgo(row.verdictAt)}` : "reviewed"}</span>;
}

function Row({ row, quiet, onReassess, busy }: {
  row: SecurityServiceRow;
  quiet: boolean;
  onReassess: (row: SecurityServiceRow) => void;
  busy: boolean;
}) {
  const canReassess = !row.rescanning && row.verdict !== WAITING;
  return (
    <tr className={quiet ? css.quietRow : undefined}>
      <td className={css.colService}>
        <Link className={css.serviceLink} to={`/services/${row.id}?tab=security`}>
          {row.name}
        </Link>
      </td>
      <td className={css.colExposure}>
        <span className="tag tag-outline">{exposureOf(row).label}</span>
      </td>
      <td className={css.colVersion}>
        <Version row={row} />
      </td>
      <td className={css.colReason}>
        {row.verdictNote ?? (row.verdict === WAITING ? "No verdict yet." : "See the card for the reasoning.")}
        {!quiet && (
          <div className={css.findingCount}>
            {row.open} finding{row.open === 1 ? "" : "s"}
            {row.kev && ", at least one known exploited"}
          </div>
        )}
      </td>
      <td className={css.colReviewed}>
        <Reviewed row={row} />
      </td>
      <td className={css.colAction}>
        {canReassess && (
          <Button
            variant="ghost"
            className={css.rowBtn}
            onClick={() => onReassess(row)}
            disabled={busy}
            title="Rescan the image, then ask Claude for a fresh verdict"
            aria-label={`Reassess ${row.name}`}
          >
            <ArrowClockwiseIcon size={13} />
            {!quiet && " Reassess"}
          </Button>
        )}
      </td>
    </tr>
  );
}

export function Security() {
  const { data, error, refresh } = useSecurity();
  const toast = useToast();
  const [verdict, setVerdict] = useState<string | null>(null);
  const [exposure, setExposure] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // a service id, or "*" for all

  const rows = useMemo(() => data?.services ?? [], [data]);
  const byExposure = exposure ? rows.filter((r) => exposureOf(r).key === exposure) : rows;
  const shown = verdict ? byExposure.filter((r) => r.verdict === verdict) : byExposure;
  const needStep = rows.filter((r) => r.verdict === "Act now" || r.verdict === "Next upgrade").length;

  async function reassess(row: SecurityServiceRow) {
    setBusy(row.id);
    try {
      const res = await api.reassessSecurity(row.id);
      toast(
        res.rescanning
          ? `Rescanning ${row.name}'s image, then Claude assesses again`
          : res.scanner
            ? `Asked Claude to assess ${row.name} again`
            : `The cve-scanner isn't running, so ${row.name}'s image wasn't rescanned. Asked Claude to assess the last scan's findings`,
      );
      refresh();
    } catch (e) {
      toast(e instanceof ApiError ? `Couldn't reassess: ${e.detail}` : "Couldn't ask Claude to reassess");
    } finally {
      setBusy(null);
    }
  }

  async function reassessAll() {
    setBusy("*");
    try {
      const res = await api.reassessAllSecurity();
      toast(`Asked Claude to assess ${res.asked} service${res.asked === 1 ? "" : "s"} again`);
      refresh();
    } catch (e) {
      toast(e instanceof ApiError ? `Couldn't reassess: ${e.detail}` : "Couldn't ask Claude to reassess");
    } finally {
      setBusy(null);
    }
  }

  const colorOf = (name: string) => rows.find((r) => r.verdict === name)?.verdictColor ?? null;

  return (
    <div className={`${views.page} ${views.wide}`}>
      <div className={views.pageHead}>
        <div>
          <h1>Security</h1>
          <p className={views.pageSub}>
            {data == null
              ? error
                ? "Couldn't load the security findings."
                : "Loading…"
              : data.scannedAt == null
                ? "No scan yet. The cve-scanner runs nightly."
                : `${rows.length} service${rows.length === 1 ? " has" : "s have"} open findings · ` +
                  `${needStep} need${needStep === 1 ? "s" : ""} a step from you · ` +
                  `last full scan ${relativeAgo(data.scannedAt)}`}
          </p>
        </div>
        {rows.length > 0 && (
          <Button
            variant="secondary"
            onClick={reassessAll}
            disabled={busy !== null}
            title="Ask Claude for a fresh verdict on every service, e.g. after changing its instructions. Doesn't rescan."
          >
            <ArrowClockwiseIcon size={15} /> Reassess all
          </Button>
        )}
      </div>

      {data && (data.errors.length > 0 || data.unscanned.length > 0) && (
        <p className={css.scanWarning}>
          <Icon name="warning" size={14} />
          {data.unscanned.length > 0 &&
            ` ${data.unscanned.length} image${data.unscanned.length === 1 ? "" : "s"} couldn't be scanned: ${data.unscanned.join(", ")}.`}
          {data.errors.length > 0 && ` ${data.errors.join(" ")}`}
        </p>
      )}

      {rows.length > 0 && (
        <div className={css.filters}>
          <div className={css.filterRow}>
            <span className={css.filterLabel}>Verdict</span>
            <button
              type="button"
              className={`${css.chip} ${verdict === null ? css.chipOn : ""}`}
              onClick={() => setVerdict(null)}
            >
              All {byExposure.length}
            </button>
            {GROUPS.map((g) => {
              const n = byExposure.filter((r) => r.verdict === g.verdict).length;
              if (n === 0) return null;
              return (
                <button
                  key={g.verdict}
                  type="button"
                  className={`${css.chip} ${verdict === g.verdict ? css.chipOn : ""}`}
                  onClick={() => setVerdict(verdict === g.verdict ? null : g.verdict)}
                  aria-pressed={verdict === g.verdict}
                >
                  <ShieldLabel color={colorOf(g.verdict)}>
                    {g.verdict === WAITING ? "Being assessed" : g.verdict} {n}
                  </ShieldLabel>
                </button>
              );
            })}
          </div>
          <div className={css.filterRow}>
            <span className={css.filterLabel}>Exposure</span>
            <button
              type="button"
              className={`${css.chip} ${exposure === null ? css.chipOn : ""}`}
              onClick={() => setExposure(null)}
            >
              Any
            </button>
            {EXPOSURES.map((e) => {
              const n = rows.filter((r) => exposureOf(r).key === e.key).length;
              if (n === 0) return null;
              return (
                <button
                  key={e.key}
                  type="button"
                  className={`${css.chip} ${exposure === e.key ? css.chipOn : ""}`}
                  onClick={() => setExposure(exposure === e.key ? null : e.key)}
                  aria-pressed={exposure === e.key}
                >
                  {e.label} {n}
                </button>
              );
            })}
          </div>
        </div>
      )}

      {data && data.scannedAt != null && rows.length === 0 && (
        <Card className={css.emptyCard}>
          <Icon name="shield-check" size={38} weight="fill" color="var(--color-ok)" />
          <div className={css.emptyTitle}>No open findings</div>
          <p className={css.emptySub}>
            The last scan of {data.images} images found nothing that clears the bar.
          </p>
        </Card>
      )}

      {GROUPS.map((g) => {
        const members = shown.filter((r) => r.verdict === g.verdict);
        if (members.length === 0) return null;
        return (
          <section key={g.verdict} className={css.group}>
            <h2 className={css.groupHead}>
              <ShieldLabel color={members[0].verdictColor}>
                {g.verdict === WAITING ? "Being assessed" : g.verdict}
              </ShieldLabel>
              <span className={css.groupHint}>
                {members.length} · {g.hint}
              </span>
            </h2>
            <Card className={css.groupCard}>
              <table className={css.groupTable}>
                <tbody>
                  {members.map((row) => (
                    <Row
                      key={row.id}
                      row={row}
                      quiet={g.quiet}
                      onReassess={reassess}
                      busy={busy !== null}
                    />
                  ))}
                </tbody>
              </table>
            </Card>
          </section>
        );
      })}
    </div>
  );
}
