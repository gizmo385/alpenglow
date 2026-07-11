/* Small shared formatting helpers (C1). Available to C2–C4.
   The backend returns ISO-8601 timestamps and raw byte/second counts; these
   turn them into the human strings the design uses ("3 days ago", "8.4 TB"). */

/** ISO timestamp (or null) → relative "ago" string, e.g. "42s ago". */
export function relativeAgo(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 14) return `${days} days ago`;
  const weeks = Math.round(days / 7);
  if (weeks < 9) return `${weeks} weeks ago`;
  const months = Math.round(days / 30);
  return `${months} months ago`;
}

/** Uptime seconds → "12d 4h" / "3h 12m" / "8m". */
export function formatUptime(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

/** Bytes → "8.4 TB" / "41.2 GB" / "512 MB", 1024-based. */
export function formatBytes(bytes: number | null | undefined, digits = 1): string {
  if (bytes == null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let n = bytes;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  const val = i === 0 ? String(Math.round(n)) : n.toFixed(digits);
  return `${val} ${units[i]}`;
}

/** Bytes → "10.9T" / "928G" / "512M", matching `zpool list`'s own base-1024
 * single-letter convention. Used by the Storage & ZFS tile only, so its numbers
 * line up with what `zpool list` reports (10.9T, not 11.99 TB). */
export function formatBytesZfs(bytes: number | null | undefined, digits = 1): string {
  if (bytes == null) return "—";
  const units = ["B", "K", "M", "G", "T", "P"];
  let n = bytes;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  const val = i === 0 ? String(Math.round(n)) : n.toFixed(digits);
  return `${val}${units[i]}`;
}
