/* Overview tab (handoff §View 3 → Overview tab).
 *
 * auto-fit minmax(240px,1fr) facts grid, then the description paragraph, then the
 * Tags editor. Facts mirror the prototype: Status (colored), Uptime, Image (mono
 * wraps), Version (current → latest, accent when an update exists), Domain,
 * Access tier, Single sign-on (label + mono detection-source sub-line), Restart
 * policy, Ports, Mem limit. */

import type { ReactNode } from "react";
import {
  ArrowClockwiseIcon,
  CircleIcon,
  ClockIcon,
  CubeIcon,
  GlobeIcon,
  LinkIcon,
  MemoryIcon,
  PlugsIcon,
  TagIcon,
} from "@phosphor-icons/react";

import type { ServiceDetail, Tier } from "../../api/types";
import { Card } from "../../components";
import { formatUptime } from "../../lib/format";
import { CategoryEditor } from "./CategoryEditor";
import { TagEditor } from "./TagEditor";
import css from "./detail.module.css";

const STATUS_LABEL: Record<ServiceDetail["status"], string> = {
  up: "Running",
  down: "Stopped",
  restarting: "Restarting…",
  updating: "Updating…",
};
const STATUS_VAR: Record<ServiceDetail["status"], string> = {
  up: "var(--color-ok)",
  down: "var(--color-down)",
  restarting: "var(--color-busy)",
  updating: "var(--color-busy)",
};

const TIER_META: Record<Tier, { label: string; icon: ReactNode }> = {
  public: { label: "Public", icon: <GlobeIcon size={13} /> },
  tailnet: { label: "Tailnet", icon: <GlobeIcon size={13} /> },
  internal: { label: "Internal (LAN)", icon: <GlobeIcon size={13} /> },
  management: { label: "Management", icon: <GlobeIcon size={13} /> },
};

type FactProps = {
  label: string;
  icon: ReactNode;
  value: ReactNode;
  valueClass?: string;
  valueStyle?: React.CSSProperties;
  sub?: string;
};

function Fact({ label, icon, value, valueClass, valueStyle, sub }: FactProps) {
  return (
    <Card className={css.fact}>
      <div className={css.factLabel}>
        {icon}
        {label}
      </div>
      <div className={`${css.factValue}${valueClass ? ` ${valueClass}` : ""}`} style={valueStyle}>
        {value}
      </div>
      {sub && <div className={css.factSub}>{sub}</div>}
    </Card>
  );
}

type OverviewTabProps = {
  svc: ServiceDetail;
  onTagsChanged: () => void;
};

export function OverviewTab({ svc, onTagsChanged }: OverviewTabProps) {
  const tier = TIER_META[svc.tier];
  const hasUpdate = Boolean(svc.latestVersion);
  const version = hasUpdate ? `${svc.currentVersion}  →  ${svc.latestVersion}` : svc.currentVersion;

  return (
    <>
      <div className={css.facts}>
        <Fact
          label="Status"
          icon={<CircleIcon size={13} />}
          value={STATUS_LABEL[svc.status]}
          valueStyle={{ color: STATUS_VAR[svc.status] }}
        />
        <Fact label="Uptime" icon={<ClockIcon size={13} />} value={formatUptime(svc.uptimeSeconds)} />
        <Fact
          label="Image"
          icon={<CubeIcon size={13} />}
          value={svc.image}
          valueClass={css.monoSmall}
        />
        <Fact
          label="Version"
          icon={<TagIcon size={13} />}
          value={version}
          valueClass={`${css.mono}${hasUpdate ? ` ${css.accent}` : ""}`}
        />
        <Fact
          label="Domain"
          icon={<LinkIcon size={13} />}
          value={svc.url ? svc.url.replace(/^https?:\/\//, "") : "— (no web ingress)"}
          valueClass={svc.url ? css.monoSmall : undefined}
        />
        <Fact label="Access tier" icon={tier.icon} value={tier.label} />
        <Fact
          label="Restart policy"
          icon={<ArrowClockwiseIcon size={13} />}
          value={svc.restartPolicy}
          valueClass={css.mono}
        />
        <Fact
          label="Ports"
          icon={<PlugsIcon size={13} />}
          value={svc.ports || "—"}
          valueClass={css.mono}
        />
        <Fact
          label="Mem limit"
          icon={<MemoryIcon size={13} />}
          value={svc.memLimit ?? "—"}
          valueClass={css.mono}
        />
      </div>

      {svc.description && <div className={css.desc}>{svc.description}</div>}

      <CategoryEditor serviceId={svc.id} category={svc.category} onChanged={onTagsChanged} />
      <TagEditor serviceId={svc.id} tags={svc.tags} onChanged={onTagsChanged} />
    </>
  );
}

export { STATUS_LABEL, STATUS_VAR };
