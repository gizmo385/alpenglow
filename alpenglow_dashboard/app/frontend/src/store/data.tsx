/* Shared polled-data context (C1).
 *
 * The Shell polls /api/services (10s) and /api/overview (30s) ONCE and shares
 * the results through this context so the Sidebar and every view read the same
 * live data without spawning duplicate polling loops. C2/C3/C4 consume it via
 * `useData()`:
 *
 *   const { services, overview, refreshServices } = useData();
 *
 * `services` / `overview` are null until the first fetch resolves. Views that
 * need their own endpoint (e.g. C4's /stats, /logs, /compose; C3's /updates)
 * still call the client/hooks directly — this context only holds the two
 * shell-level polls that the sidebar also needs.
 */

import { createContext, useContext } from "react";

import type { Overview, ServiceSummary } from "../api/types";

export type DataContextValue = {
  services: ServiceSummary[];
  servicesError: Error | null;
  servicesLoading: boolean;
  refreshServices: () => void;

  overview: Overview | null;
  overviewError: Error | null;
  overviewLoading: boolean;
  refreshOverview: () => void;
};

export const DataContext = createContext<DataContextValue | null>(null);

/** Access the shell-level polled data. Must be used under the Shell. */
export function useData(): DataContextValue {
  const ctx = useContext(DataContext);
  if (!ctx) throw new Error("useData must be used within the Shell");
  return ctx;
}
