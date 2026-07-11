/* Placeholder shell (work package A1).
   Renders /api/services grouped by category to prove the contract round-trips.
   C1 replaces this with the real Shell/Sidebar + routed views. */

import { CircleNotchIcon, MountainsIcon, WarningIcon } from "@phosphor-icons/react";
import { useEffect, useState } from "react";

import { api } from "./api/client";
import type { ServiceSummary, Status } from "./api/types";

const CATEGORY_ORDER = ["Media", "Productivity", "Home", "Infrastructure", "Monitoring"];

const STATUS_COLOR: Record<Status, string> = {
  up: "var(--color-ok)",
  down: "var(--color-down)",
  restarting: "var(--color-busy)",
  updating: "var(--color-busy)",
};

export default function App() {
  const [services, setServices] = useState<ServiceSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .services()
      .then(setServices)
      .catch((e: Error) => setError(e.message));
  }, []);

  const categories = services
    ? CATEGORY_ORDER.filter((c) => services.some((s) => s.category === c))
    : [];

  return (
    <div style={{ display: "flex", height: "100vh", overflow: "hidden" }}>
      <aside
        style={{
          width: 222,
          flex: "none",
          padding: "18px 14px",
          borderRight: "1px solid var(--color-divider)",
          background: "linear-gradient(180deg, var(--color-sidebar-top), var(--color-bg))",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span
            style={{
              display: "grid",
              placeItems: "center",
              width: 30,
              height: 30,
              borderRadius: 9,
              background:
                "radial-gradient(circle at 30% 25%, var(--color-accent-400), var(--color-accent-700))",
              boxShadow: "0 0 16px -4px var(--color-accent)",
            }}
          >
            <MountainsIcon size={17} weight="fill" color="var(--color-sidebar-top)" />
          </span>
          <div style={{ lineHeight: 1.05 }}>
            <div style={{ fontWeight: 600, fontSize: 16 }}>Alpenglow</div>
            <div
              style={{
                fontSize: 10,
                letterSpacing: "var(--tracking-caps)",
                textTransform: "uppercase",
                color: "var(--color-neutral-500)",
              }}
            >
              home server
            </div>
          </div>
        </div>
        <p style={{ marginTop: 20, fontSize: 12, color: "var(--color-neutral-500)" }}>
          A1 scaffold — shell, views and primitives land in C1–C4.
        </p>
      </aside>

      <main style={{ flex: 1, overflowY: "auto", padding: "26px 30px 60px" }}>
        <h2>Services</h2>
        {error && (
          <p style={{ color: "var(--color-down)", display: "flex", alignItems: "center", gap: 8 }}>
            <WarningIcon size={16} /> {error}
          </p>
        )}
        {!services && !error && (
          <p style={{ color: "var(--color-neutral-500)", display: "flex", alignItems: "center", gap: 8 }}>
            <CircleNotchIcon size={16} style={{ animation: "spin 0.9s linear infinite" }} /> loading…
          </p>
        )}
        {services &&
          categories.map((cat) => (
            <section key={cat} style={{ marginTop: "var(--space-6)" }}>
              <h4 style={{ fontSize: 15, marginBottom: "var(--space-4)" }}>
                {cat}{" "}
                <span style={{ fontSize: 12, color: "var(--color-neutral-500)" }}>
                  {services.filter((s) => s.category === cat).length}
                </span>
              </h4>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fill, minmax(268px, 1fr))",
                  gap: 12,
                }}
              >
                {services
                  .filter((s) => s.category === cat)
                  .map((s) => (
                    <div
                      key={s.id}
                      style={{
                        background: "var(--color-surface)",
                        borderRadius: "var(--radius-md)",
                        boxShadow: "var(--shadow-sm)",
                        padding: "13px 14px",
                        borderLeft: `3px solid ${
                          s.status === "down"
                            ? "var(--color-down)"
                            : s.latestVersion
                              ? "var(--color-accent-600)"
                              : "var(--color-neutral-800)"
                        }`,
                      }}
                    >
                      <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
                        <span
                          style={{
                            width: 9,
                            height: 9,
                            borderRadius: "50%",
                            background: STATUS_COLOR[s.status],
                            flex: "none",
                          }}
                        />
                        <span style={{ fontWeight: 500, fontSize: 14.5 }}>{s.name}</span>
                        {s.latestVersion && (
                          <span
                            style={{
                              marginLeft: "auto",
                              fontSize: 11,
                              padding: "2px 7px",
                              borderRadius: 5,
                              background: "var(--color-accent-800)",
                              color: "var(--color-accent-100)",
                            }}
                          >
                            ⬆ {s.latestVersion}
                          </span>
                        )}
                      </div>
                      <div
                        className="mono"
                        style={{
                          fontSize: 11,
                          color: "var(--color-neutral-500)",
                          marginTop: 6,
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {s.image}
                      </div>
                    </div>
                  ))}
              </div>
            </section>
          ))}
      </main>
    </div>
  );
}
