/* Security tab: the service's open findings from the cve-scanner, Claude's
 * verdict and latest reply from its Security card, a box to ask Claude a
 * follow-up (an @claude comment on the card), and earlier closed cards.
 *
 * Data: GET /api/services/{id}/security, polled every 60s so a reply shows up
 * without a reload. */

import { useState } from "react";
import { ArrowClockwiseIcon, ArrowSquareOutIcon, PaperPlaneRightIcon } from "@phosphor-icons/react";

import { api, ApiError } from "../../api/client";
import { useServiceSecurity } from "../../api/hooks";
import type { SecurityFinding, ServiceSecurity } from "../../api/types";
import { Button } from "../../components";
import { relativeAgo } from "../../lib/format";
import { inline, Markdown } from "../../lib/markdown";
import { useToast } from "../../store/toast";
import { ShieldPill } from "../security/Shield";
import css from "./detail.module.css";
import sec from "../security/security.module.css";

const TIER_TEXT: Record<string, string> = {
  public: "Public: reachable from the internet",
  tailnet: "Tailnet only",
  management: "Tailnet only (management)",
  internal: "No ingress",
};

const SEVERITY_COLOR: Record<string, string> = {
  Critical: "#e08a8a",
  High: "#f0a070",
  Medium: "#f5b942",
};

/** Split Claude's reply into its verdict sentence, the details and the
 *  dispatcher's footer ("— Claude, security profile · 7 turns · …"). */
export function splitReply(reply: string): { lead: string; body: string; footer: string } {
  const lines = reply.replace(/\r/g, "").split("\n");
  let lead = "";
  let footer = "";
  const first = lines.findIndex((l) => l.trim());
  const m = first >= 0 ? /^\W*verdict\W*(act now|next upgrade|waiting on upstream|not affected)[\s*_.:\-–—]*(.*)$/i.exec(lines[first].trim()) : null;
  if (m) {
    lead = m[2].replace(/[*_]+$/, "").trim();
    lines.splice(first, 1);
  }
  let last = lines.length - 1;
  while (last >= 0 && !lines[last].trim()) last--;
  if (last >= 0 && /^[—-]\s/.test(lines[last].trim()) && /claude/i.test(lines[last])) {
    footer = lines[last].trim().replace(/^[—-]\s*/, "");
    lines.splice(last, 1);
  }
  return { lead, body: lines.join("\n").trim(), footer };
}

function Reply({ text }: { text: string }) {
  const { lead, body, footer } = splitReply(text);
  return (
    <div className={sec.replyBox}>
      {lead && <p className={sec.lead}>{inline(lead, "lead")}</p>}
      {body && <Markdown text={body} className={sec.reply} />}
      {footer && <div className={sec.footer}>{footer}</div>}
    </div>
  );
}

function Finding({ f }: { f: SecurityFinding }) {
  const sevColor = SEVERITY_COLOR[f.severity] ?? "var(--color-neutral-400)";
  return (
    <li className={sec.finding}>
      <div className={sec.findingHead}>
        {f.url ? (
          <a className={sec.findingId} href={f.url} target="_blank" rel="noopener noreferrer">
            {f.id}
          </a>
        ) : (
          <span className={sec.findingId}>{f.id}</span>
        )}
        <span className={sec.chip} style={{ color: sevColor, borderColor: sevColor }}>
          {f.severity}
        </span>
        {f.kev && (
          <span className={`${sec.chip} ${sec.kev}`} title="On CISA's known-exploited list">
            Known exploited
          </span>
        )}
        {f.epss != null && (
          <span className={sec.muted} title="Chance of exploitation in the next 30 days (EPSS)">
            EPSS {(f.epss * 100).toFixed(1)}%
          </span>
        )}
      </div>
      <div className={sec.findingPkg}>
        {f.package} {f.version}{" "}
        {f.fixedIn.length > 0 ? (
          <span className={sec.fix}>→ fixed in {f.fixedIn.join(", ")}</span>
        ) : (
          <span className={sec.muted}>no fix released yet</span>
        )}
      </div>
      {f.summary && <div className={sec.summary}>{f.summary}</div>}
      <div className={sec.muted}>
        {f.type} in {f.image}
        {f.aliases.length > 0 && ` · also ${f.aliases.join(", ")}`}
        {f.ignoredReason ? ` · ignored: ${f.ignoredReason}` : ` · ${f.why}`}
      </div>
    </li>
  );
}

/** Ask Claude for a fresh verdict on the card. The scanner looks at the
 *  service's running image again first, so an upgrade since the nightly scan
 *  is what gets assessed. */
function Reassess({ serviceId, onAsked }: { serviceId: string; onAsked: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);

  async function submit() {
    setBusy(true);
    try {
      const res = await api.reassessSecurity(serviceId);
      toast(
        res.rescanning
          ? "Rescanning the image first, then Claude assesses again; the new verdict lands here"
          : "Asked Claude to assess again; the new verdict lands here in a few minutes",
      );
      onAsked();
    } catch (e) {
      toast(e instanceof ApiError ? `Couldn't reassess: ${e.detail}` : "Couldn't ask Claude to reassess");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Button variant="ghost" className={sec.cardLink} onClick={submit} disabled={busy} title="Ask Claude for a fresh verdict">
      <ArrowClockwiseIcon size={13} /> Reassess
    </Button>
  );
}

function Ask({ serviceId, onAsked }: { serviceId: string; onAsked: () => void }) {
  const toast = useToast();
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    if (!question.trim()) return;
    setBusy(true);
    try {
      await api.askSecurity(serviceId, question);
      setQuestion("");
      toast("Asked Claude; the reply lands here in a few minutes");
      onAsked();
    } catch (e) {
      toast(e instanceof ApiError ? `Couldn't ask: ${e.detail}` : "Couldn't ask Claude");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={sec.ask}>
      <textarea
        className={`input ${sec.askInput}`}
        rows={2}
        placeholder="Ask Claude a follow-up, e.g. “We don't enable SAML; does that change it?”"
        value={question}
        maxLength={2000}
        onChange={(e) => setQuestion(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void submit();
        }}
      />
      <Button variant="secondary" onClick={submit} disabled={busy || !question.trim()}>
        <PaperPlaneRightIcon size={13} /> Ask
      </Button>
    </div>
  );
}

function Body({ data, refresh }: { data: ServiceSecurity; refresh: () => void }) {
  if (!data.scannedAt) {
    return (
      <div className={css.loadingNote}>
        No scan yet. The cve-scanner runs nightly; see cve-scanner/README.md.
      </div>
    );
  }
  const failed = data.images.filter((i) => i.error);
  const canReassess = data.canAsk && !data.rescanning && data.verdict !== "Waiting for assessment";
  return (
    <>
      <div className={`card elev-sm ${sec.section}`}>
        <div className={sec.verdictRow}>
          {data.verdict ? (
            <ShieldPill verdict={data.verdict} color={data.verdictColor} />
          ) : (
            <ShieldPill verdict="No open findings" color="#6bd39a" />
          )}
          <span className={sec.muted}>
            {TIER_TEXT[data.tier] ?? data.tier} · scanned {relativeAgo(data.scannedAt)}
            {data.verdictAt && ` · assessed ${relativeAgo(data.verdictAt)}`}
            {data.belowBar > 0 && ` · ${data.belowBar} lower-risk findings not shown`}
          </span>
          {canReassess && <Reassess serviceId={data.id} onAsked={refresh} />}
          {data.cardUrl && (
            <a
              style={canReassess ? { marginLeft: 0 } : undefined}
              className={`btn btn-ghost ${sec.cardLink}`} href={data.cardUrl} target="_blank" rel="noopener noreferrer">
              Open card <ArrowSquareOutIcon size={13} />
            </a>
          )}
        </div>
        {data.rescanning && (
          <div className={sec.muted}>
            Rescanning this service's image. Claude assesses again as soon as that's done.
          </div>
        )}
        {data.reply && data.verdict === "Waiting for assessment" && (
          <div className={sec.muted}>
            The findings or the running image changed since this reply, so Claude is assessing again. Its earlier
            reply is below.
          </div>
        )}
        {data.reply ? (
          <Reply text={data.reply} />
        ) : (
          data.open.length > 0 && <div className={sec.muted}>Claude is assessing these findings.</div>
        )}
        {data.canAsk && <Ask serviceId={data.id} onAsked={refresh} />}
      </div>

      {data.open.length > 0 && (
        <div className={`card elev-sm ${sec.section}`}>
          <div className="card-kicker">Open findings ({data.open.length})</div>
          <ul className={sec.findings}>
            {data.open.map((f) => (
              <Finding key={`${f.id} ${f.image}`} f={f} />
            ))}
          </ul>
        </div>
      )}

      {failed.length > 0 && (
        <div className={`card elev-sm ${sec.section}`}>
          <div className="card-kicker">Not scanned</div>
          {failed.map((i) => (
            <div key={i.ref} className={sec.muted}>
              <b>{i.ref}</b>: {i.error}
            </div>
          ))}
        </div>
      )}

      {data.ignored.length > 0 && (
        <details className={`card elev-sm ${sec.section}`}>
          <summary className="card-kicker">Ignored ({data.ignored.length})</summary>
          <ul className={sec.findings}>
            {data.ignored.map((f) => (
              <Finding key={`${f.id} ${f.image}`} f={f} />
            ))}
          </ul>
        </details>
      )}

      {data.closed.length > 0 && (
        <div className={`card elev-sm ${sec.section}`}>
          <div className="card-kicker">Earlier cards</div>
          {data.closed.map((c) => (
            <div key={`${c.title} ${c.url}`} className={sec.muted}>
              {c.url ? (
                <a href={c.url} target="_blank" rel="noopener noreferrer">
                  {c.title}
                </a>
              ) : (
                c.title
              )}
            </div>
          ))}
        </div>
      )}
    </>
  );
}

export function SecurityTab({ serviceId }: { serviceId: string }) {
  const { data, error, refresh } = useServiceSecurity(serviceId);
  if (error && !data) return <div className={css.loadingNote}>Security findings unavailable.</div>;
  if (!data) return <div className={css.loadingNote}>Loading security findings…</div>;
  return (
    <div className={sec.tab}>
      <Body data={data} refresh={refresh} />
    </div>
  );
}
