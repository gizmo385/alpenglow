/* Cross-service tag management panel (G1).
 *
 * Opened from the Overview tag-filter row's "Manage" ghost button. A
 * Nocturne-styled modal (surface card, --shadow-lg, toastIn entrance) listing
 * every tag in the store with its usage count and member service names, an
 * inline rename-across-services, and delete-from-all-services with a confirm
 * step showing the affected count. SSO-special tags are manageable like any
 * other tag — renaming/deleting one loses its special accent chip rendering, so
 * a subtle warning line is shown while acting on one. */

import { useEffect, useState } from "react";
import { CheckIcon, PencilSimpleIcon, TrashIcon, WarningIcon, XIcon } from "@phosphor-icons/react";

import { api } from "../../api/client";
import type { TagInfo } from "../../api/types";
import { useData } from "../../store/data";
import { useToast } from "../../store/toast";
import "./tagmanager.css";

type TagManagerProps = {
  onClose: () => void;
};

export function TagManager({ onClose }: TagManagerProps) {
  const toast = useToast();
  const { services, refreshServices } = useData();
  const [tags, setTags] = useState<TagInfo[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);

  // Map service id → display name so member lists read nicely.
  const nameOf = (id: string) => services.find((s) => s.id === id)?.name ?? id;

  async function load() {
    try {
      setTags(await api.tags());
    } catch {
      toast("Failed to load tags");
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Close on Escape.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function doRename(name: string) {
    const to = renameDraft.trim();
    setRenaming(null);
    if (!to || to === name) return;
    setBusy(true);
    try {
      const res = await api.renameTag({ name, to });
      toast(`Renamed “${name}” → “${to}” on ${res.changed.length} service(s)`);
      await load();
      refreshServices();
    } catch {
      toast("Failed to rename tag");
    } finally {
      setBusy(false);
    }
  }

  async function doDelete(name: string) {
    setConfirmDelete(null);
    setBusy(true);
    try {
      const res = await api.deleteTag({ name });
      toast(`Removed “${name}” from ${res.changed.length} service(s)`);
      await load();
      refreshServices();
    } catch {
      toast("Failed to delete tag");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="tm-backdrop" onClick={onClose} role="presentation">
      <div
        className="tm-panel"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Manage tags"
      >
        <div className="tm-head">
          <h3 className="tm-title">Manage tags</h3>
          <button type="button" className="btn btn-icon tm-close" onClick={onClose} aria-label="Close">
            <XIcon size={16} />
          </button>
        </div>
        <p className="tm-sub">
          Rename or delete a tag across every service that carries it. Changes are
          immediate and audit-logged.
        </p>

        <div className="tm-list">
          {tags === null && <div className="tm-empty">Loading tags…</div>}
          {tags !== null && tags.length === 0 && (
            <div className="tm-empty">No tags in use yet.</div>
          )}
          {tags?.map((t) => (
            <div key={t.name} className="tm-row">
              <div className="tm-row-main">
                {renaming === t.name ? (
                  <input
                    className="input tm-rename-input"
                    autoFocus
                    value={renameDraft}
                    onChange={(e) => setRenameDraft(e.target.value)}
                    onBlur={() => void doRename(t.name)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                      if (e.key === "Escape") setRenaming(null);
                    }}
                    aria-label={`Rename tag ${t.name}`}
                  />
                ) : (
                  <span className={`tm-tag-name${t.sso ? " tm-tag-sso" : ""}`}>{t.name}</span>
                )}
                <span className="tm-count">
                  {t.count} service{t.count === 1 ? "" : "s"}
                </span>
                <span className="tm-actions">
                  <button
                    type="button"
                    className="btn btn-icon"
                    title="Rename across services"
                    disabled={busy}
                    onClick={() => {
                      setRenaming(t.name);
                      setRenameDraft(t.name);
                      setConfirmDelete(null);
                    }}
                  >
                    <PencilSimpleIcon size={14} />
                  </button>
                  <button
                    type="button"
                    className="btn btn-icon tm-del"
                    title="Delete from all services"
                    disabled={busy}
                    onClick={() => {
                      setConfirmDelete((c) => (c === t.name ? null : t.name));
                      setRenaming(null);
                    }}
                  >
                    <TrashIcon size={14} />
                  </button>
                </span>
              </div>

              <div className="tm-members">{t.services.map(nameOf).join(" · ")}</div>

              {t.sso && (renaming === t.name || confirmDelete === t.name) && (
                <div className="tm-warn">
                  <WarningIcon size={12} weight="fill" />
                  “{t.name}” is a special SSO tag — {renaming === t.name ? "renaming" : "deleting"}{" "}
                  it loses its accent SSO chip styling on cards.
                </div>
              )}

              {confirmDelete === t.name && (
                <div className="tm-confirm">
                  <span>
                    Remove “{t.name}” from {t.count} service{t.count === 1 ? "" : "s"}?
                  </span>
                  <button
                    type="button"
                    className="btn btn-secondary tm-confirm-yes"
                    disabled={busy}
                    onClick={() => void doDelete(t.name)}
                  >
                    <CheckIcon size={13} /> Delete
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost"
                    onClick={() => setConfirmDelete(null)}
                  >
                    Cancel
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
