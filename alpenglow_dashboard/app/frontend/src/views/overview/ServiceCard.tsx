/* Service card (handoff §View 1 service card).
 * 3px priority border, status dot (pulses when busy), name, update pill, mono
 * image line, SSO + tier chips, tag chips, divider, footer with Open + Manage.
 * The whole card navigates to /services/{id}; Open opens the service URL in a
 * new tab (stopPropagation so it doesn't also open detail). */

import type { MouseEvent } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowFatUpIcon, ArrowSquareOutIcon } from "@phosphor-icons/react";

import type { ServiceSummary } from "../../api/types";
import { Card, StatusDot } from "../../components";
import { Icon } from "../../lib/icons";
import { formatUptime } from "../../lib/format";
import { SSO_TAG_META, STATUS_LABEL, TIER_META } from "./meta";
import "./overview.css";

function priorityClass(s: ServiceSummary): string {
  if (s.status === "down") return "pri-down";
  if (s.latestVersion) return "pri-update";
  return "";
}

export function ServiceCard({ service }: { service: ServiceSummary }) {
  const navigate = useNavigate();
  const s = service;

  const busy = s.status === "restarting" || s.status === "updating";
  const tier = TIER_META[s.tier];
  const hasUpdate = Boolean(s.latestVersion);

  // SSO tags render as accent chips (key / shield-star) alongside the tier chip;
  // all other tags render as plain outlined chips in the tag row.
  const ssoTags = s.tags.filter((t) => t in SSO_TAG_META);
  const plainTags = s.tags.filter((t) => !(t in SSO_TAG_META));

  const openService = (e: MouseEvent) => {
    e.stopPropagation();
  };

  return (
    <Card
      hover
      elevation="sm"
      className={`ov-card ${priorityClass(s)}`}
      role="button"
      tabIndex={0}
      onClick={() => navigate(`/services/${s.id}`)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          navigate(`/services/${s.id}`);
        }
      }}
    >
      {/* row 1: status dot · name · update pill */}
      <div className="ov-card-row1">
        <StatusDot status={s.status} size={9} busy={busy} />
        <span className="ov-card-name" title={s.name}>
          {s.name}
        </span>
        {hasUpdate && (
          <span className="tag tag-accent ov-update-pill">
            <ArrowFatUpIcon size={10} />
            {s.latestVersion}
          </span>
        )}
      </div>

      {/* image ref */}
      <div className="ov-card-image" title={s.image}>
        {s.image}
      </div>

      {/* SSO + tier chips */}
      <div className="ov-card-chips">
        {ssoTags.map((t) => (
          <span key={t} className="tag tag-accent ov-sso-chip" title={t}>
            <Icon name={SSO_TAG_META[t].icon} size={10} weight="fill" />
            {t}
          </span>
        ))}
        <span className="tag tag-neutral ov-tier-chip">
          <Icon name={tier.icon} size={10} />
          {tier.label}
        </span>
      </div>

      {/* tag chips (non-SSO tags) */}
      {plainTags.length > 0 && (
        <div className="ov-card-tags">
          {plainTags.map((t) => (
            <span key={t} className="ov-card-tag">
              {t}
            </span>
          ))}
        </div>
      )}

      {/* footer: status · uptime · Open · Manage */}
      <div className="ov-card-foot">
        <span className="ov-card-status">
          {STATUS_LABEL[s.status]} · {formatUptime(s.uptimeSeconds)}
        </span>
        {s.url && (
          <a
            className="btn btn-ghost ov-open-btn"
            href={s.url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={openService}
            title="Open service"
          >
            <ArrowSquareOutIcon size={15} />
          </a>
        )}
        <span className="btn btn-secondary ov-manage-btn">Manage</span>
      </div>
    </Card>
  );
}
