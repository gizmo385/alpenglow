/* SSO-tag, tier-chip and status presentation maps (the prototype's
 * ssoView/tierView/statusView). Kept local to the Overview view per the C2
 * file-ownership rule.
 *
 * F1: SSO status is no longer auto-detected — it lives in the tag system. The
 * accent chip visual language survives, driven by two special tag values:
 * "Keycloak SSO" (accent key chip) and "Identity provider" (accent shield-star
 * chip). SSO_TAG_META maps those tag strings to their chip glyph; every other
 * tag renders as a plain outlined chip. */

import type { Status, Tier } from "../../api/types";

/** The two special SSO tags and their accent-chip glyphs (fill weight). */
export const KEYCLOAK_SSO_TAG = "Keycloak SSO";
export const IDENTITY_PROVIDER_TAG = "Identity provider";

export type SsoTagMeta = { icon: string };

/** Tags that render as the accent SSO chip instead of a plain outlined chip. */
export const SSO_TAG_META: Record<string, SsoTagMeta> = {
  [KEYCLOAK_SSO_TAG]: { icon: "key" },
  [IDENTITY_PROVIDER_TAG]: { icon: "shield-star" },
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

/** Fallback glyph for user-created categories not in CATEGORY_ICON. */
export const CATEGORY_ICON_FALLBACK = "stack";

/** Curated icon set for the category icon picker (G1). Includes the five known
 *  category glyphs plus sensible extras; all resolve via lib/icons. */
export const CATEGORY_ICON_CHOICES = [
  "play-circle",
  "briefcase",
  "house-line",
  "stack",
  "pulse",
  "images",
  "cloud",
  "database",
  "shield-check",
  "brain",
  "gauge",
  "heartbeat",
  "folder-open",
  "lightning",
  "archive",
  "globe",
];

/** The glyph for a category: known icon, else the fallback (ph-stack). */
export function categoryIcon(cat: string): string {
  return CATEGORY_ICON[cat] ?? CATEGORY_ICON_FALLBACK;
}

/** Categories in display order: the five known ones first (fixed order), then
 *  any user-created categories present in the data, alphabetically. */
export function orderCategories(present: Iterable<string>): string[] {
  const set = new Set(present);
  const known = CATEGORY_ORDER.filter((c) => set.has(c));
  const extras = [...set].filter((c) => !CATEGORY_ORDER.includes(c)).sort();
  return [...known, ...extras];
}
