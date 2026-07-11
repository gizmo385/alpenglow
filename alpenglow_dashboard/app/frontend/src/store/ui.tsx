/* Shared UI filter state (C1) — the Overview filter contract for C2–C4.
 *
 * ┌─────────────────────────────────────────────────────────────────────────┐
 * │ WHERE OVERVIEW FILTER STATE LIVES (read this if you are C2/C3/C4)         │
 * │                                                                          │
 * │ The search query, active category, and active tag filter live in this   │
 * │ single React context (provided once in App.tsx, above the router). Any   │
 * │ view can read/write them via `useUiState()`. They are intentionally NOT  │
 * │ inside the Overview view so that:                                        │
 * │   • the Sidebar (C1) can toggle the category filter and reflect the      │
 * │     active category, and                                                 │
 * │   • the Overview view (C2) can consume query × category × tag to filter  │
 * │     and group its service cards.                                         │
 * │                                                                          │
 * │ Semantics (match the prototype):                                         │
 * │   • query   — live substring filter over name / image / category.        │
 * │   • category — a single category name or null (= "All"). Sidebar rows    │
 * │     toggle it (click active row again → null). `setCategory` is a        │
 * │     toggle: passing the already-active category clears it.               │
 * │   • tag     — a single active tag or null; toggle semantics too.         │
 * │   • Filters COMPOSE (AND): query AND category AND tag.                    │
 * │                                                                          │
 * │ C2 should build the tag-filter row from the union of tags in use and     │
 * │ call setTag(t) (toggle). Navigating away does not auto-clear filters;    │
 * │ clear them explicitly if a view wants a clean slate.                     │
 * └─────────────────────────────────────────────────────────────────────────┘
 */

import { createContext, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";

export type UiState = {
  /** Live search substring (name / image / category). */
  query: string;
  setQuery: (q: string) => void;

  /** Active category filter, or null for "All". */
  category: string | null;
  /** Toggle: passing the already-active category clears the filter. */
  setCategory: (c: string | null) => void;

  /** Active tag filter, or null. */
  tag: string | null;
  /** Toggle: passing the already-active tag clears the filter. */
  setTag: (t: string | null) => void;

  /** Active running/stopped status filter, or null. Composes (AND) with the
   *  query/category/tag filters. Driven by the Services stat-tile legend. */
  status: "up" | "down" | null;
  /** Toggle: passing the already-active status clears the filter. */
  setStatus: (s: "up" | "down" | null) => void;
};

const UiContext = createContext<UiState | null>(null);

export function UiStateProvider({ children }: { children: ReactNode }) {
  const [query, setQuery] = useState("");
  const [category, setCategoryRaw] = useState<string | null>(null);
  const [tag, setTagRaw] = useState<string | null>(null);
  const [status, setStatusRaw] = useState<"up" | "down" | null>(null);

  const value = useMemo<UiState>(
    () => ({
      query,
      setQuery,
      category,
      setCategory: (c) => setCategoryRaw((prev) => (prev === c ? null : c)),
      tag,
      setTag: (t) => setTagRaw((prev) => (prev === t ? null : t)),
      status,
      setStatus: (s) => setStatusRaw((prev) => (prev === s ? null : s)),
    }),
    [query, category, tag, status],
  );

  return <UiContext.Provider value={value}>{children}</UiContext.Provider>;
}

/** Access the shared Overview filter state. Must be under <UiStateProvider>. */
export function useUiState(): UiState {
  const ctx = useContext(UiContext);
  if (!ctx) throw new Error("useUiState must be used within <UiStateProvider>");
  return ctx;
}
