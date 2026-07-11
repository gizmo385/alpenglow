/* Hand-written TypeScript mirror of backend models.py — the API contract.
   Keep in lockstep with alpenglow_dashboard/models.py; do not drift. */

export type Status = "up" | "down" | "restarting" | "updating";
export type Tier = "public" | "tailnet" | "internal" | "management";
export type ActionName = "restart" | "stop" | "start" | "pull";

// GET /api/health
export interface Health {
  status: string;
  version: string;
}

// GET /api/me
export interface Me {
  user: string;
  groups: string[];
  isAdmin: boolean;
}

// GET /api/csrf
export interface Csrf {
  token: string;
}

// GET /api/overview
export interface OverviewServices {
  up: number;
  down: number;
  total: number;
}

export interface OverviewUpdates {
  count: number;
}

export interface MonitorRef {
  name: string;
  status: string; // up | down | pending | maintenance | unknown
}

export interface OverviewMonitors {
  up: number | null;
  total: number | null;
  note: string | null;
  monitors: MonitorRef[];
  url: string | null;
}

export interface OverviewBackups {
  pgAgo: string | null;
  kopiaAgo: string | null;
  pgAt: string | null;
  kopiaAt: string | null;
  ok: boolean | null;
}

export interface OverviewHost {
  load1: number | null;
  load5: number | null;
  load15: number | null;
  cpuPct: number | null;
  memUsed: number | null;
  memTotal: number | null;
  swapUsed: number | null;
  swapTotal: number | null;
}

export interface StoragePool {
  name: string;
  state: string;
  used: number | null; // usable used (parity excluded)
  size: number | null; // usable total (parity excluded)
  rawUsed: number | null; // raw zpool alloc (incl. parity)
  rawSize: number | null; // raw zpool size (incl. parity)
  scrubAgo: string | null;
  errors: number | null;
}

export interface StorageFs {
  label: string;
  used: number | null;
  size: number | null;
  pct: number | null;
}

export interface OverviewStorage {
  pools: StoragePool[];
  fs: StorageFs[];
}

export interface OverviewMeta {
  host: string;
  branch: string;
}

export interface Overview {
  services: OverviewServices;
  updates: OverviewUpdates;
  monitors: OverviewMonitors;
  backups: OverviewBackups;
  host: OverviewHost;
  storage: OverviewStorage;
  polledAt: string;
  meta: OverviewMeta;
}

// GET /api/services , /api/services/{id}
export interface ContainerRef {
  name: string;
  status: string;
}

export interface ServiceSummary {
  id: string;
  name: string;
  category: string;
  icon: string;
  description: string;
  image: string;
  currentVersion: string;
  latestVersion: string | null;
  releasedAt: string | null;
  changelogUrl: string | null;
  status: Status;
  uptimeSeconds: number | null;
  url: string | null;
  tier: Tier;
  containers: ContainerRef[];
  tags: string[];
  restartPolicy: string;
  ports: string;
  memLimit: string | null;
}

export interface ServiceDetail extends ServiceSummary {
  composePath: string;
  projectName: string;
  primaryContainer: string | null;
}

// GET /api/services/{id}/stats
export interface StatPoint {
  t: number;
  v: number;
}

export interface StatsHistory {
  cpu: StatPoint[];
  mem: StatPoint[];
}

export interface StatsCurrent {
  cpuPct: number | null;
  memUsed: number | null;
  memLimit: number | null;
  netIO: string | null;
  blockIO: string | null;
  pids: number | null;
  restarts: number | null;
}

export interface Stats {
  history: StatsHistory;
  current: StatsCurrent;
}

// PUT /api/services/{id}/settings
// Partial update: omit a field to leave it unchanged. `category: null` clears
// the per-service override (falling back to the metadata default).
export interface SettingsRequest {
  tags?: string[];
  category?: string | null;
}

export interface SettingsPayload {
  tags: string[];
  category: string | null;
}

// POST /api/services/{id}/actions
export interface ActionRequest {
  action: ActionName;
  confirm?: boolean;
}

export interface ActionResponse {
  accepted: boolean;
  jobId: string;
}

// GET /api/updates
export interface UpdateEntry {
  id: string;
  name: string;
  icon: string;
  image: string;
  currentVersion: string;
  latestVersion: string;
  releasedAt: string | null;
  changelogUrl: string | null;
}

export interface Updates {
  services: UpdateEntry[];
  lastUpdated: string | null;
  refreshing: boolean;
  trackerUnavailable: boolean;
}
