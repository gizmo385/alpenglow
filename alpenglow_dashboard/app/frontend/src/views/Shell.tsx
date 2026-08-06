/* App shell (C1): fixed 222px sidebar + scrolling main.
   Owns the shell-level polling (services 10s, overview 30s) and shares it via
   DataContext so the sidebar and routed views never double-poll.

   Responsive: below 820px the sidebar collapses into an off-canvas drawer.
   A slim top bar with a hamburger toggles it; a backdrop + route changes +
   Escape all dismiss it. On wider screens the top bar/backdrop are hidden and
   the sidebar is the usual fixed rail. */

import { useEffect, useState } from "react";
import { ListIcon, MountainsIcon } from "@phosphor-icons/react";
import { Outlet, useLocation } from "react-router-dom";

import { useCategories, useOverview, useServices } from "../api/hooks";
import { DataContext } from "../store/data";
import type { DataContextValue } from "../store/data";
import { CommandPalette } from "./CommandPalette";
import { Sidebar } from "./Sidebar";
import styles from "./Shell.module.css";

export function Shell() {
  const services = useServices();
  const overview = useOverview();
  const categories = useCategories();

  const [navOpen, setNavOpen] = useState(false);
  const location = useLocation();

  // Dismiss the drawer whenever the route changes (nav link / service open).
  useEffect(() => {
    setNavOpen(false);
  }, [location.pathname]);

  // Escape closes the drawer.
  useEffect(() => {
    if (!navOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setNavOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navOpen]);

  const value: DataContextValue = {
    services: services.data ?? [],
    servicesError: services.error,
    servicesLoading: services.loading,
    refreshServices: services.refresh,
    overview: overview.data,
    overviewError: overview.error,
    overviewLoading: overview.loading,
    refreshOverview: overview.refresh,
    categories: categories.data ?? [],
    refreshCategories: categories.refresh,
  };

  return (
    <DataContext.Provider value={value}>
      <div className={styles.shell}>
        <header className={styles.topbar}>
          <button
            type="button"
            className={styles.menuBtn}
            onClick={() => setNavOpen(true)}
            aria-label="Open navigation"
            aria-expanded={navOpen}
          >
            <ListIcon size={20} />
          </button>
          <span className={styles.topbarBrand}>
            <span className={styles.topbarTile}>
              <MountainsIcon size={14} weight="fill" color="var(--color-sidebar-top)" />
            </span>
            Alpenglow
          </span>
        </header>

        <Sidebar
          services={value.services}
          meta={value.overview?.meta ?? null}
          open={navOpen}
          onNavigate={() => setNavOpen(false)}
        />
        {navOpen && (
          <div
            className={styles.backdrop}
            onClick={() => setNavOpen(false)}
            aria-hidden="true"
          />
        )}

        <main className={`${styles.main} ag-scroll`}>
          <Outlet />
        </main>
      </div>
      <CommandPalette services={value.services} />
    </DataContext.Provider>
  );
}
