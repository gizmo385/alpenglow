/* App shell (C1): fixed 222px sidebar + scrolling main.
   Owns the shell-level polling (services 10s, overview 30s) and shares it via
   DataContext so the sidebar and routed views never double-poll. */

import { Outlet } from "react-router-dom";

import { useCategories, useOverview, useServices } from "../api/hooks";
import { DataContext } from "../store/data";
import type { DataContextValue } from "../store/data";
import { Sidebar } from "./Sidebar";
import styles from "./Shell.module.css";

export function Shell() {
  const services = useServices();
  const overview = useOverview();
  const categories = useCategories();

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
        <Sidebar services={value.services} meta={value.overview?.meta ?? null} />
        <main className={`${styles.main} ag-scroll`}>
          <Outlet />
        </main>
      </div>
    </DataContext.Provider>
  );
}
