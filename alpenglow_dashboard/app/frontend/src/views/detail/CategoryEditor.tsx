/* Category editor (F1: categories are user-customizable).
 *
 * Shows the service's effective category with an inline editor: pick an
 * existing category (datalist built from the live services poll) or type a new
 * one; Enter commits. Persists via PUT /api/services/{id}/settings. */

import { useMemo, useState } from "react";

import { api } from "../../api/client";
import { useData } from "../../store/data";
import { useToast } from "../../store/toast";
import css from "./detail.module.css";

type CategoryEditorProps = {
  serviceId: string;
  category: string;
  /** Refresh the shared services poll so sidebar/groups reflect the change. */
  onChanged: () => void;
};

export function CategoryEditor({ serviceId, category, onChanged }: CategoryEditorProps) {
  const toast = useToast();
  const { services } = useData();
  const [draft, setDraft] = useState(category);

  // Re-sync when the poll delivers a different category for this service.
  const [seen, setSeen] = useState(category);
  if (category !== seen) {
    setSeen(category);
    setDraft(category);
  }

  const known = useMemo(
    () => Array.from(new Set(services.map((s) => s.category))).sort(),
    [services],
  );

  async function save() {
    const next = draft.trim();
    if (!next || next === category) {
      setDraft(category);
      return;
    }
    try {
      await api.putSettings(serviceId, { category: next });
      toast(`Moved to “${next}”`);
      onChanged();
    } catch {
      toast("Failed to save category");
      setDraft(category);
    }
  }

  return (
    <div className={css.tagsSection}>
      <div className={css.tagsHeading}>Category</div>
      <div className={css.suggestRow}>
        <input
          className={`input ${css.tagInput}`}
          list="category-options"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => void save()}
          onKeyDown={(e) => {
            if (e.key === "Enter") void save();
            if (e.key === "Escape") setDraft(category);
          }}
          aria-label="Service category"
        />
        <datalist id="category-options">
          {known.map((c) => (
            <option key={c} value={c} />
          ))}
        </datalist>
        <span className={css.addLabel}>pick an existing category or type a new one</span>
      </div>
    </div>
  );
}
