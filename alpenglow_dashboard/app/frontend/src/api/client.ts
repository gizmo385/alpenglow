/* Typed API client. Every call maps 1:1 to a backend route.
   Mutating calls fetch + attach the CSRF double-submit token automatically. */

import type {
  ActionRequest,
  ActionResponse,
  BeszelContainer,
  CategoryIconRequest,
  CategoryInfo,
  CategoryOrderRequest,
  CategoryRenameRequest,
  Csrf,
  Health,
  HostCharts,
  Me,
  Overview,
  ServiceDetail,
  ServiceSummary,
  SettingsPayload,
  SettingsRequest,
  Stats,
  TagDeleteRequest,
  TagInfo,
  TagMutationResult,
  TagRenameRequest,
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
  hostCharts: () => request<HostCharts>("/api/host/charts"),
  services: () => request<ServiceSummary[]>("/api/services"),
  service: (id: string) => request<ServiceDetail>(`/api/services/${id}`),
  stats: (id: string) => request<Stats>(`/api/services/${id}/stats`),
  beszelContainers: (id: string) =>
    request<BeszelContainer[]>(`/api/services/${id}/beszel`),
  compose: (id: string) => request<string>(`/api/services/${id}/compose`),
  putSettings: (id: string, patch: SettingsRequest) =>
    mutate<SettingsPayload>(`/api/services/${id}/settings`, "PUT", patch),
  action: (id: string, action: ActionRequest) =>
    mutate<ActionResponse>(`/api/services/${id}/actions`, "POST", action),
  updates: () => request<Updates>("/api/updates"),
  refreshUpdates: () => mutate<Updates>("/api/updates/refresh", "POST"),

  // Category management (G1)
  categories: () => request<CategoryInfo[]>("/api/categories"),
  renameCategory: (body: CategoryRenameRequest) =>
    mutate<TagMutationResult>("/api/categories/rename", "POST", body),
  setCategoryIcon: (body: CategoryIconRequest) =>
    mutate<CategoryIconRequest>("/api/categories/icon", "POST", body),
  setCategoryOrder: (body: CategoryOrderRequest) =>
    mutate<CategoryOrderRequest>("/api/categories/order", "POST", body),

  // Cross-service tag management (G1)
  tags: () => request<TagInfo[]>("/api/tags"),
  renameTag: (body: TagRenameRequest) =>
    mutate<TagMutationResult>("/api/tags/rename", "POST", body),
  deleteTag: (body: TagDeleteRequest) =>
    mutate<TagMutationResult>("/api/tags/delete", "POST", body),
  /** SSE log stream; caller owns the EventSource lifecycle. */
  logStream: (id: string) => new EventSource(`/api/services/${id}/logs`),
};
