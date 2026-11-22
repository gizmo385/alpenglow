/* Security verdict shield: a colored shield and the verdict, used on service
 * cards, the Updates table and the Security tab. Colors come from the API
 * (security/verdicts.py GROUPS): Act now red, Next upgrade amber, Not
 * affected green, Waiting gray. */

import type { ReactNode } from "react";
import { ShieldIcon } from "@phosphor-icons/react";

import styles from "./security.module.css";

const FALLBACK = "var(--color-neutral-500)";

/** Pill: tinted background, colored shield and label. */
export function ShieldPill({ verdict, color, title }: {
  verdict: string;
  color?: string | null;
  title?: string;
}) {
  const c = color ?? FALLBACK;
  return (
    <span
      className={styles.pill}
      style={{ color: c, background: `color-mix(in srgb, ${c} 16%, transparent)` }}
      title={title ?? `Security: ${verdict}`}
    >
      <ShieldIcon size={12} weight="fill" aria-hidden />
      {verdict}
    </span>
  );
}

/** Inline: colored shield, then the label in body text color. */
export function ShieldLabel({ color, children }: {
  color?: string | null;
  children: ReactNode;
}) {
  return (
    <span className={styles.label}>
      <ShieldIcon size={13} weight="fill" style={{ color: color ?? FALLBACK }} aria-hidden />
      <span>{children}</span>
    </span>
  );
}
