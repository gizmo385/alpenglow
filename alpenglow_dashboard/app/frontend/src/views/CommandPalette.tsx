/* Command palette / quickswitcher (⌘K, Ctrl-K).
 *
 * A global overlay — mounted once in the Shell so it works on every page — that
 * fuzzy-searches services by name, image, category and container name and jumps
 * to the service detail page. Fully keyboard-driven: ⌘K/Ctrl-K toggles it,
 * ↑/↓ move, ↵ opens, Esc closes. It reads the already-polled services list from
 * the Shell, so it adds no new fetching. */

import { useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { useNavigate } from "react-router-dom";
import { MagnifyingGlassIcon } from "@phosphor-icons/react";

import type { ServiceSummary } from "../api/types";
import { StatusDot } from "../components";
import { Icon } from "../lib/icons";
import "./commandpalette.css";

const MAX_RESULTS = 8;

/** A service matches when the query is a substring of any searchable field;
 *  results are ranked by the earliest match position, then name. */
function search(services: ServiceSummary[], query: string): ServiceSummary[] {
  const q = query.trim().toLowerCase();
  return services
    .map((s) => {
      const hay = `${s.name} ${s.image} ${s.category} ${s.containers
        .map((c) => c.name)
        .join(" ")}`.toLowerCase();
      return { s, idx: q ? hay.indexOf(q) : 0 };
    })
    .filter((r) => r.idx >= 0)
    .sort((a, b) => a.idx - b.idx || a.s.name.localeCompare(b.s.name))
    .slice(0, MAX_RESULTS)
    .map((r) => r.s);
}

export function CommandPalette({ services }: { services: ServiceSummary[] }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // Global toggle: ⌘K / Ctrl-K from anywhere.
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        setOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Reset + focus each time it opens.
  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActive(0);
    const id = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => window.clearTimeout(id);
  }, [open]);

  const results = useMemo(() => search(services, query), [services, query]);

  // Clamp the selection when the result set shrinks, and keep it in view.
  useEffect(() => {
    setActive((a) => Math.min(a, Math.max(0, results.length - 1)));
  }, [results.length]);
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(".cmdk-item.active")?.scrollIntoView({ block: "nearest" });
  }, [active, results]);

  if (!open) return null;

  const go = (s: ServiceSummary | undefined) => {
    if (!s) return;
    navigate(`/services/${s.id}`);
    setOpen(false);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") {
      setOpen(false);
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => Math.min(results.length - 1, a + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(0, a - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      go(results[active]);
    }
  };

  return (
    <div className="cmdk-backdrop" onMouseDown={() => setOpen(false)}>
      <div className="cmdk" onMouseDown={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
        <div className="cmdk-search">
          <MagnifyingGlassIcon size={16} className="cmdk-search-icon" />
          <input
            ref={inputRef}
            className="cmdk-input"
            placeholder="Jump to a service or container…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
            aria-label="Search services"
          />
        </div>
        <div className="cmdk-list" ref={listRef}>
          {results.length === 0 ? (
            <div className="cmdk-empty">No matches</div>
          ) : (
            results.map((s, i) => (
              <button
                key={s.id}
                type="button"
                className={`cmdk-item${i === active ? " active" : ""}`}
                onMouseEnter={() => setActive(i)}
                onClick={() => go(s)}
              >
                <Icon name={s.icon} size={16} color="var(--color-accent-300)" />
                <span className="cmdk-name">{s.name}</span>
                <span className="cmdk-cat">{s.category}</span>
                <StatusDot status={s.status} size={8} />
              </button>
            ))
          )}
        </div>
        <div className="cmdk-foot">
          <span><kbd>↑</kbd><kbd>↓</kbd> navigate</span>
          <span><kbd>↵</kbd> open</span>
          <span><kbd>esc</kbd> close</span>
        </div>
      </div>
    </div>
  );
}
