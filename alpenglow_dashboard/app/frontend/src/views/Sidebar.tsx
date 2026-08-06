/* Sidebar (C1 + G1) — handoff §Sidebar. 222px, brand tile + primary nav +
   category filter list + host/branch footer. Category rows toggle the shared UI
   category filter (see store/ui.tsx).

   G1: the Categories list is also the management surface. A ghost pencil next to
   the "Categories" label toggles edit mode; each row then exposes rename (inline
   input), an icon picker (curated Phosphor set), and up/down ordering. Order and
   icons are persisted server-side (/api/categories) and consumed here (stored
   order first, then the known-five defaults, then alphabetical; icon fallback
   stored → known-five → ph-stack, all resolved by the backend). */

import { useMemo, useState } from "react";
import {
  ArrowsClockwiseIcon,
  CaretDownIcon,
  CaretUpIcon,
  CheckIcon,
  MountainsIcon,
  PencilSimpleIcon,
  SquaresFourIcon,
} from "@phosphor-icons/react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";

import type { OverviewMeta, ServiceSummary } from "../api/types";
import { api } from "../api/client";
import { Icon } from "../lib/icons";
import { useData } from "../store/data";
import { useToast } from "../store/toast";
import { useUiState } from "../store/ui";
import { CATEGORY_ICON_CHOICES, categoryIcon, orderCategories } from "./overview/meta";
import styles from "./Sidebar.module.css";

type SidebarProps = {
  services: ServiceSummary[];
  meta: OverviewMeta | null;
  /** Drawer open state (mobile only; ignored by the desktop fixed rail). */
  open?: boolean;
  /** Called when a nav target is chosen, so the shell can close the drawer. */
  onNavigate?: () => void;
};

export function Sidebar({ services, meta, open = false, onNavigate }: SidebarProps) {
  const { category, setCategory } = useUiState();
  const { categories: catInfos, refreshCategories, refreshServices } = useData();
  const toast = useToast();
  const navigate = useNavigate();
  const location = useLocation();

  const [editing, setEditing] = useState(false);
  const [iconFor, setIconFor] = useState<string | null>(null); // row with icon picker open
  const [renameDraft, setRenameDraft] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  const updateCount = services.filter((s) => s.latestVersion).length;
  const countFor = (cat: string) => services.filter((s) => s.category === cat).length;
  const onOverview = location.pathname === "/";

  // Prefer the server's category ordering/icons (stored order → known-five →
  // alphabetical, resolved backend-side). Fall back to deriving from the live
  // services list before /api/categories resolves. In edit mode, show every
  // category the server knows about (incl. empty-but-metadata ones).
  const categories = useMemo(() => {
    if (catInfos.length > 0) {
      return catInfos
        .filter((c) => editing || c.count > 0)
        .map((c) => ({ name: c.name, icon: c.icon, count: c.count }));
    }
    return orderCategories(services.map((s) => s.category)).map((name) => ({
      name,
      icon: categoryIcon(name),
      count: countFor(name),
    }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [catInfos, services, editing]);

  async function commitRename(name: string) {
    const to = (renameDraft[name] ?? "").trim();
    setRenameDraft((d) => {
      const next = { ...d };
      delete next[name];
      return next;
    });
    if (!to || to === name) return;
    setBusy(true);
    try {
      const res = await api.renameCategory({ name, to });
      toast(`Renamed “${name}” → “${to}” (${res.changed.length} moved)`);
      refreshCategories();
      refreshServices();
    } catch {
      toast("Failed to rename category");
    } finally {
      setBusy(false);
    }
  }

  async function pickIcon(name: string, icon: string) {
    setIconFor(null);
    setBusy(true);
    try {
      await api.setCategoryIcon({ name, icon });
      refreshCategories();
    } catch {
      toast("Failed to set icon");
    } finally {
      setBusy(false);
    }
  }

  async function move(index: number, dir: -1 | 1) {
    const names = categories.map((c) => c.name);
    const j = index + dir;
    if (j < 0 || j >= names.length) return;
    [names[index], names[j]] = [names[j], names[index]];
    setBusy(true);
    try {
      await api.setCategoryOrder({ order: names });
      refreshCategories();
    } catch {
      toast("Failed to reorder");
    } finally {
      setBusy(false);
    }
  }

  return (
    <aside className={`${styles.sidebar} ${open ? styles.open : ""}`}>
      <div className={styles.brand}>
        <span className={styles.brandTile}>
          <MountainsIcon size={17} weight="fill" color="var(--color-sidebar-top)" />
        </span>
        <div className={styles.wordmark}>
          <div className={styles.name}>Alpenglow</div>
          <div className={styles.tagline}>home&nbsp;server</div>
        </div>
      </div>

      <nav className={styles.nav}>
        <NavLink
          to="/"
          className={({ isActive }) => `${styles.navItem} ${isActive ? styles.navActive : ""}`}
          onClick={() => onNavigate?.()}
          end
        >
          <SquaresFourIcon size={17} />
          <span className={styles.navLabel}>Overview</span>
        </NavLink>
        <NavLink
          to="/updates"
          className={({ isActive }) => `${styles.navItem} ${isActive ? styles.navActive : ""}`}
          onClick={() => onNavigate?.()}
        >
          <ArrowsClockwiseIcon size={17} />
          <span className={styles.navLabel}>Updates</span>
          {updateCount > 0 && <span className={styles.badge}>{updateCount}</span>}
        </NavLink>
      </nav>

      <div className={styles.catBlock}>
        <div className={styles.catHeadRow}>
          <span className={styles.catHeading}>Categories</span>
          <button
            type="button"
            className={`${styles.catEditBtn} ${editing ? styles.catEditActive : ""}`}
            onClick={() => {
              setEditing((e) => !e);
              setIconFor(null);
            }}
            title={editing ? "Done editing categories" : "Edit categories"}
            aria-pressed={editing}
            aria-label="Edit categories"
          >
            {editing ? <CheckIcon size={13} /> : <PencilSimpleIcon size={13} />}
          </button>
        </div>

        <div className={styles.catList}>
          {categories.map((c, i) => {
            const active = onOverview && category === c.name;
            if (editing) {
              return (
                <div key={c.name} className={styles.catEditRow}>
                  <div className={styles.catEditMain}>
                    <button
                      type="button"
                      className={styles.catIconBtn}
                      onClick={() => setIconFor((n) => (n === c.name ? null : c.name))}
                      title="Change icon"
                      disabled={busy}
                    >
                      <Icon name={c.icon} size={14} className={styles.catIcon} />
                    </button>
                    <input
                      className={`input ${styles.catRenameInput}`}
                      value={renameDraft[c.name] ?? c.name}
                      onChange={(e) =>
                        setRenameDraft((d) => ({ ...d, [c.name]: e.target.value }))
                      }
                      onBlur={() => void commitRename(c.name)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                        if (e.key === "Escape")
                          setRenameDraft((d) => {
                            const n = { ...d };
                            delete n[c.name];
                            return n;
                          });
                      }}
                      aria-label={`Rename category ${c.name}`}
                    />
                    <span className={styles.catReorder}>
                      <button
                        type="button"
                        className={styles.catMoveBtn}
                        onClick={() => void move(i, -1)}
                        disabled={busy || i === 0}
                        aria-label="Move up"
                      >
                        <CaretUpIcon size={12} />
                      </button>
                      <button
                        type="button"
                        className={styles.catMoveBtn}
                        onClick={() => void move(i, 1)}
                        disabled={busy || i === categories.length - 1}
                        aria-label="Move down"
                      >
                        <CaretDownIcon size={12} />
                      </button>
                    </span>
                  </div>
                  {iconFor === c.name && (
                    <div className={styles.iconPicker}>
                      {CATEGORY_ICON_CHOICES.map((glyph) => (
                        <button
                          key={glyph}
                          type="button"
                          className={`${styles.iconChoice} ${
                            glyph === c.icon ? styles.iconChoiceActive : ""
                          }`}
                          onClick={() => void pickIcon(c.name, glyph)}
                          title={glyph}
                        >
                          <Icon name={glyph} size={15} />
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              );
            }
            return (
              <button
                key={c.name}
                type="button"
                className={`${styles.catItem} ${active ? styles.catActive : ""}`}
                onClick={() => {
                  if (!onOverview) navigate("/");
                  setCategory(c.name);
                  onNavigate?.();
                }}
              >
                <Icon name={c.icon} size={14} className={styles.catIcon} />
                <span className={styles.catLabel}>{c.name}</span>
                <span className={styles.catCount}>{c.count}</span>
              </button>
            );
          })}
        </div>
      </div>

      <div className={styles.footer}>
        <div className={styles.footerRow}>
          <Icon name="hard-drives" size={13} />
          {meta?.host ?? "…"} · Denver
        </div>
        <div className={styles.footerRow}>
          <Icon name="git-branch" size={13} />
          alpenglow @ {meta?.branch ?? "…"}
        </div>
      </div>
    </aside>
  );
}
