/* SSO-chip, tier-chip and status presentation maps (handoff §SSO model + the
 * prototype's ssoView/tierView/statusView). Kept local to the Overview view
 * per the C2 file-ownership rule; each entry mirrors the prototype's
 * label/icon/title/style exactly. */

import type { SSOState, Status, Tier } from "../../api/types";

export type SsoMeta = {
  label: string;
  title: string;
  icon: string;
  /** "accent" | "neutral" | "none" — maps to a chip variant/style. */
  kind: "accent" | "neutral" | "none";
  /** Phosphor weight for the glyph (fill for key/shield-star). */
  weight?: "regular" | "fill";
};

/** SSO chip presentation, one entry per handoff state. */
export const SSO_META: Record<SSOState, SsoMeta> = {
  keycloak: {
    label: "Keycloak",
    title: "SSO via Keycloak",
    icon: "key",
    kind: "accent",
    weight: "fill",
  },
  oidc: {
    label: "OIDC",
    title: "OIDC login via Keycloak",
    icon: "key",
    kind: "accent",
    weight: "fill",
  },
  self: {
    label: "Identity provider",
    title: "This is the SSO provider",
    icon: "shield-star",
    kind: "accent",
    weight: "fill",
  },
  native: {
    label: "Native auth",
    title: "Own login, no SSO",
    icon: "user",
    kind: "neutral",
  },
  none: {
    label: "No SSO",
    title: "Not linked to Keycloak",
    icon: "warning",
    kind: "none",
  },
};

export type TierMeta = { label: string; icon: string };

/** Access-tier chip presentation (prototype tierView). */
export const TIER_META: Record<Tier, TierMeta> = {
  public: { label: "Public", icon: "globe" },
  tailnet: { label: "Tailnet", icon: "shield" },
  internal: { label: "Local only", icon: "house" },
  management: { label: "Management", icon: "wrench" },
};

export type StatusMeta = { label: string };

/** Footer status label (prototype statusView). */
export const STATUS_LABEL: Record<Status, string> = {
  up: "Running",
  down: "Stopped",
  restarting: "Restarting…",
  updating: "Updating…",
};

/** Display order for category groups (prototype CAT_ORDER). */
export const CATEGORY_ORDER = [
  "Media",
  "Productivity",
  "Home",
  "Infrastructure",
  "Monitoring",
];

/** Category header glyphs (prototype CAT_META). */
export const CATEGORY_ICON: Record<string, string> = {
  Media: "play-circle",
  Productivity: "briefcase",
  Home: "house-line",
  Infrastructure: "stack",
  Monitoring: "pulse",
};
