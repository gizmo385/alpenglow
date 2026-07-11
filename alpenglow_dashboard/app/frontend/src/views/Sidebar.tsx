/* Sidebar (C1) — handoff §Sidebar. 222px, brand tile + primary nav + category
   filter list + host/branch footer. Category rows toggle the shared UI
   category filter (see store/ui.tsx). */

import { useMemo } from "react";
import { ArrowsClockwiseIcon, MountainsIcon, SquaresFourIcon } from "@phosphor-icons/react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";

import type { OverviewMeta, ServiceSummary } from "../api/types";
import { Icon } from "../lib/icons";
import { useUiState } from "../store/ui";
import { categoryIcon, orderCategories } from "./overview/meta";
import styles from "./Sidebar.module.css";

type SidebarProps = {
  services: ServiceSummary[];
  meta: OverviewMeta | null;
};

export function Sidebar({ services, meta }: SidebarProps) {
  const { category, setCategory } = useUiState();
  const navigate = useNavigate();
  const location = useLocation();

  const updateCount = services.filter((s) => s.latestVersion).length;
  const countFor = (cat: string) => services.filter((s) => s.category === cat).length;
  const onOverview = location.pathname === "/";

  // Category list is fully dynamic from live data: the five known categories
  // first (fixed order + icons), then any user-created categories alphabetically
  // with the fallback glyph. Only categories actually present are shown.
  const categories = useMemo(
    () =>
      orderCategories(services.map((s) => s.category)).map((name) => ({
        name,
        icon: categoryIcon(name),
      })),
    [services],
  );

  return (
    <aside className={styles.sidebar}>
      <div className={styles.brand}>
        <span className={styles.brandTile}>
          <MountainsIcon size={17} weight="fill" color="var(--color-sidebar-top)" />
        </span>
        <div className={styles.wordmark}>
          <div className={styles.name}>Alpenglow</div>
          <div className={styles.tagline}>home&nbsp;server</div>
        </div>
      </div>

      <nav className={styles.nav}>
        <NavLink
          to="/"
          className={({ isActive }) => `${styles.navItem} ${isActive ? styles.navActive : ""}`}
          end
        >
          <SquaresFourIcon size={17} />
          <span className={styles.navLabel}>Overview</span>
        </NavLink>
        <NavLink
          to="/updates"
          className={({ isActive }) => `${styles.navItem} ${isActive ? styles.navActive : ""}`}
        >
          <ArrowsClockwiseIcon size={17} />
          <span className={styles.navLabel}>Updates</span>
          {updateCount > 0 && <span className={styles.badge}>{updateCount}</span>}
        </NavLink>
      </nav>

      <div className={styles.catBlock}>
        <div className={styles.catHeading}>Categories</div>
        <div className={styles.catList}>
          {categories.map((c) => {
            const active = onOverview && category === c.name;
            return (
              <button
                key={c.name}
                type="button"
                className={`${styles.catItem} ${active ? styles.catActive : ""}`}
                onClick={() => {
                  // Category filter only makes sense on Overview; route there first.
                  if (!onOverview) navigate("/");
                  setCategory(c.name);
                }}
              >
                <Icon name={c.icon} size={14} className={styles.catIcon} />
                <span className={styles.catLabel}>{c.name}</span>
                <span className={styles.catCount}>{countFor(c.name)}</span>
              </button>
            );
          })}
        </div>
      </div>

      <div className={styles.footer}>
        <div className={styles.footerRow}>
          <Icon name="hard-drives" size={13} />
          {meta?.host ?? "…"} · Denver
        </div>
        <div className={styles.footerRow}>
          <Icon name="git-branch" size={13} />
          alpenglow @ {meta?.branch ?? "…"}
        </div>
      </div>
    </aside>
  );
}
