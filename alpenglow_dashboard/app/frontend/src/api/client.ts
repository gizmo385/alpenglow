/* Typed API client. Every call maps 1:1 to a backend route.
   Mutating calls fetch + attach the CSRF double-submit token automatically. */

import type {
  ActionRequest,
  ActionResponse,
  Csrf,
  Health,
  Me,
  Overview,
  ServiceDetail,
  ServiceSummary,
  SettingsPayload,
  SettingsRequest,
  Stats,
  Updates,
} from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(`API ${status}: ${detail}`);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    credentials: "same-origin",
    ...init,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(resp.status, detail);
  }
  const contentType = resp.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    return (await resp.json()) as T;
  }
  return (await resp.text()) as T;
}

let csrfToken: string | null = null;

async function csrf(): Promise<string> {
  if (csrfToken === null) {
    csrfToken = (await request<Csrf>("/api/csrf")).token;
  }
  return csrfToken;
}

async function mutate<T>(path: string, method: "POST" | "PUT", body?: unknown): Promise<T> {
  return request<T>(path, {
    method,
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": await csrf(),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export const api = {
  health: () => request<Health>("/api/health"),
  me: () => request<Me>("/api/me"),
  overview: () => request<Overview>("/api/overview"),
  services: () => request<ServiceSummary[]>("/api/services"),
  service: (id: string) => request<ServiceDetail>(`/api/services/${id}`),
  stats: (id: string) => request<Stats>(`/api/services/${id}/stats`),
  compose: (id: string) => request<string>(`/api/services/${id}/compose`),
  putSettings: (id: string, patch: SettingsRequest) =>
    mutate<SettingsPayload>(`/api/services/${id}/settings`, "PUT", patch),
  action: (id: string, action: ActionRequest) =>
    mutate<ActionResponse>(`/api/services/${id}/actions`, "POST", action),
  updates: () => request<Updates>("/api/updates"),
  refreshUpdates: () => mutate<Updates>("/api/updates/refresh", "POST"),
  /** SSE log stream; caller owns the EventSource lifecycle. */
  logStream: (id: string) => new EventSource(`/api/services/${id}/logs`),
};
