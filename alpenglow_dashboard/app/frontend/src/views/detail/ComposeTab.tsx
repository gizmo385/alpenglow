/* Compose tab (handoff §View 3 → Compose tab, screenshots/05).
 *
 * Card header "▤ {id}/compose.yaml" + a Copy ghost button; body is a mono <pre>
 * on #12131f showing the real compose.yaml from GET /compose. */

import { useEffect, useState } from "react";
import { CopyIcon, FileTextIcon } from "@phosphor-icons/react";

import { api } from "../../api/client";
import { Button } from "../../components";
import { useToast } from "../../store/toast";
import css from "./detail.module.css";

export function ComposeTab({ serviceId }: { serviceId: string }) {
  const toast = useToast();
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let alive = true;
    setText(null);
    setError(false);
    api
      .compose(serviceId)
      .then((t) => {
        if (alive) setText(t);
      })
      .catch(() => {
        if (alive) setError(true);
      });
    return () => {
      alive = false;
    };
  }, [serviceId]);

  async function copy() {
    if (text == null) return;
    try {
      await navigator.clipboard.writeText(text);
      toast("compose.yaml copied");
    } catch {
      toast("Copy failed");
    }
  }

  return (
    <div className={`card elev-sm ${css.panel}`}>
      <div className={css.panelHead}>
        <FileTextIcon size={14} />
        {serviceId}/compose.yaml
        <Button variant="ghost" className={css.copyBtn} onClick={copy} disabled={text == null}>
          <CopyIcon size={13} /> Copy
        </Button>
      </div>
      {error ? (
        <div className={css.loadingNote}>compose.yaml unavailable.</div>
      ) : text == null ? (
        <div className={css.loadingNote}>Loading compose.yaml…</div>
      ) : (
        <pre className={`ag-scroll ${css.composeBody}`}>{text}</pre>
      )}
    </div>
  );
}
