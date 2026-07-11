/* Overview view (C2) — handoff §View 1.
 *
 * Layout: header (summary + "polled {ago}") → stat-tile grid → Services section
 * (search + tag-filter row + per-category card groups). All three filters
 * (query × category × tag) come from the shared UI store and compose (AND);
 * the Sidebar writes the category, so navigating a category filters here. */

import { useMemo } from "react";
import { MagnifyingGlassIcon, TagIcon } from "@phosphor-icons/react";

import { useData } from "../store/data";
import { useUiState } from "../store/ui";
import { relativeAgo } from "../lib/format";
import { Icon } from "../lib/icons";
import { Input } from "../components";
import { StatTiles } from "./overview/StatTiles";
import { ServiceCard } from "./overview/ServiceCard";
import {
  KEYCLOAK_SSO_TAG,
  categoryIcon,
  orderCategories,
} from "./overview/meta";
import styles from "./views.module.css";
import "./overview/overview.css";

export function Overview() {
  const { services, overview, servicesLoading } = useData();
  const { query, category, tag, status, setQuery, setTag } = useUiState();

  // Header summary counts (prefer authoritative overview numbers, fall back to
  // the services list so the line still renders before /api/overview resolves).
  const total = overview?.services.total ?? services.length;
  const down =
    overview?.services.down ?? services.filter((s) => s.status === "down").length;
  const updates =
    overview?.updates.count ?? services.filter((s) => s.latestVersion).length;
  // "linked to Keycloak" now counts services carrying the "Keycloak SSO" tag.
  const linked = services.filter((s) => s.tags.includes(KEYCLOAK_SSO_TAG)).length;

  const summary =
    `${total} services · ${linked} linked to Keycloak · ${updates} updates pending` +
    (down ? ` · ${down} stopped` : "");

  // Tag-filter chips: the union of tags currently in use, sorted.
  const usedTags = useMemo(() => {
    const set = new Set<string>();
    for (const s of services) for (const t of s.tags) set.add(t);
    return [...set].sort();
  }, [services]);

  // Compose the four filters (AND): query × category × tag × status.
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return services.filter((s) => {
      if (category && s.category !== category) return false;
      if (tag && !s.tags.includes(tag)) return false;
      if (status && (status === "down" ? s.status !== "down" : s.status === "down"))
        return false;
      if (q) {
        const hay = `${s.name} ${s.image} ${s.category}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [services, query, category, tag, status]);

  const groups = useMemo(() => {
    return orderCategories(filtered.map((s) => s.category))
      .map((cat) => ({
        name: cat,
        icon: categoryIcon(cat),
        items: filtered.filter((s) => s.category === cat),
      }))
      .filter((g) => g.items.length > 0);
  }, [filtered]);

  const hasResults = filtered.length > 0;

  return (
    <div className={styles.page}>
      <div className={styles.pageHead}>
        <div>
          <h2>Overview</h2>
          <p className={styles.pageSub}>{summary}</p>
        </div>
        {overview && (
          <div className={styles.polled}>
            <span className={styles.polledDot} />
            polled {relativeAgo(overview.polledAt)}
          </div>
        )}
      </div>

      <StatTiles overview={overview} />

      <div className="ov-svc-head">
        <h3>Services</h3>
        <Input
          icon={<MagnifyingGlassIcon size={14} />}
          placeholder="Filter services…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Filter services"
          wrapperClassName="ov-search"
        />
      </div>

      {usedTags.length > 0 && (
        <div className="ov-tagrow">
          <span className="ov-tagrow-label">
            <TagIcon size={12} /> Tags
          </span>
          {usedTags.map((t) => (
            <button
              key={t}
              type="button"
              className={`ov-tagchip${tag === t ? " active" : ""}`}
              onClick={() => setTag(t)}
              aria-pressed={tag === t}
            >
              {t}
            </button>
          ))}
        </div>
      )}

      {hasResults ? (
        <div className="ov-groups">
          {groups.map((g) => (
            <section key={g.name}>
              <div className="ov-group-head">
                <Icon name={g.icon} size={16} color="var(--color-accent-300)" />
                <h4>{g.name}</h4>
                <span className="ov-group-count">{g.items.length}</span>
                <span className="ov-group-rule" />
              </div>
              <div className="ov-grid">
                {g.items.map((s) => (
                  <ServiceCard key={s.id} service={s} />
                ))}
              </div>
            </section>
          ))}
        </div>
      ) : (
        <div className="ov-empty">
          <MagnifyingGlassIcon size={26} className="ov-empty-icon" />
          {servicesLoading && services.length === 0
            ? "Loading services…"
            : query
              ? `No services match “${query}”.`
              : "No services match the current filters."}
        </div>
      )}
    </div>
  );
}
