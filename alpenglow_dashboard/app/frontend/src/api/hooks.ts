/* Polling data hooks on top of the A1 typed client.
 *
 * - useServices()  polls /api/services every 10s
 * - useOverview()  polls /api/overview every 30s
 * - useUpdates()   polls /api/updates every 30s (Updates view; shared here so
 *                  C3 doesn't reinvent the polling loop)
 *
 * All pause while the tab is hidden (document.hidden) and resume — with an
 * immediate refresh — when it becomes visible again, so we never poll a
 * background tab. Each hook exposes { data, error, loading, refresh }.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "./client";
import type { Overview, ServiceSummary, Updates } from "./types";

type Poll<T> = {
  data: T | null;
  error: Error | null;
  loading: boolean;
  /** Force an immediate re-fetch. */
  refresh: () => void;
};

function usePolling<T>(fetcher: () => Promise<T>, intervalMs: number): Poll<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);

  // Keep the latest fetcher without retriggering the effect on every render.
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const load = useCallback(async () => {
    try {
      const next = await fetcherRef.current();
      setData(next);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e : new Error(String(e)));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null;

    const start = () => {
      if (timer !== null) return;
      timer = setInterval(load, intervalMs);
    };
    const stop = () => {
      if (timer !== null) {
        clearInterval(timer);
        timer = null;
      }
    };

    const onVisibility = () => {
      if (document.hidden) {
        stop();
      } else {
        void load(); // catch up immediately on return
        start();
      }
    };

    void load(); // initial fetch
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [load, intervalMs]);

  return { data, error, loading, refresh: load };
}

/** /api/services, polled every 10s (paused while the tab is hidden). */
export function useServices(): Poll<ServiceSummary[]> {
  return usePolling(api.services, 10_000);
}

/** /api/overview, polled every 30s (paused while the tab is hidden). */
export function useOverview(): Poll<Overview> {
  return usePolling(api.overview, 30_000);
}

/** /api/updates, polled every 30s (paused while the tab is hidden). */
export function useUpdates(): Poll<Updates> {
  return usePolling(api.updates, 30_000);
}
