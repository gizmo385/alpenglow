/* Logs tab (handoff §View 3 → Logs tab, screenshots/04).
 *
 * Card header "⌗ docker logs -f --tail 100 {id}" + a pulsing "● streaming" dot;
 * body is a mono block on #12131f, one row per line = muted timestamp + message.
 * Error lines tint red, success lines green.
 *
 * Wired to the SSE endpoint GET /api/services/{id}/logs via EventSource:
 *   - autoscroll: pinned to bottom unless the user has scrolled up
 *   - reconnect: EventSource retries automatically; onerror flips a visible
 *     "reconnecting…" state and onopen clears it. We keep the accumulated lines.
 * The EventSource lifecycle is owned here and torn down on unmount / id change. */

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { TerminalWindowIcon } from "@phosphor-icons/react";

import { api } from "../../api/client";
import css from "./detail.module.css";

const MAX_LINES = 500;

type LogLine = {
  key: number;
  time: string;
  message: string;
  tone: "" | "error" | "success";
};

const ERROR_RE = /\b(error|fatal|econnrefused|exited with code [1-9]|panic|failed|exception)\b/i;
const OK_RE = /\b(listening|started|starting|ready|completed|success|healthy|loaded)\b/i;

/** Parse a raw SSE log line ("container | HH:MM:SS message") into parts. */
function parseLine(raw: string, key: number): LogLine {
  // Drop the "container | " prefix the multiplexer adds, if present.
  let rest = raw;
  const bar = raw.indexOf(" | ");
  if (bar !== -1) rest = raw.slice(bar + 3);

  let time = "";
  let message = rest;
  const m = rest.match(/^(\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+(.*)$/s);
  if (m) {
    time = m[1];
    message = m[2];
  }

  const tone: LogLine["tone"] = ERROR_RE.test(message)
    ? "error"
    : OK_RE.test(message)
      ? "success"
      : "";
  return { key, time, message, tone };
}

export function LogsTab({ serviceId }: { serviceId: string }) {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [reconnecting, setReconnecting] = useState(false);
  const bodyRef = useRef<HTMLDivElement>(null);
  // Track whether the user is pinned to the bottom (autoscroll) or scrolled up.
  const pinnedRef = useRef(true);
  const keyRef = useRef(0);

  useEffect(() => {
    setLines([]);
    setReconnecting(false);
    keyRef.current = 0;
    pinnedRef.current = true;

    const es = api.logStream(serviceId);

    es.addEventListener("log", (ev) => {
      const data = (ev as MessageEvent).data as string;
      if (!data) return;
      setLines((prev) => {
        const next = [...prev, parseLine(data, keyRef.current++)];
        return next.length > MAX_LINES ? next.slice(next.length - MAX_LINES) : next;
      });
    });

    // EventSource reconnects on its own; surface the gap while it does.
    es.onopen = () => setReconnecting(false);
    es.onerror = () => {
      // readyState CONNECTING (0) means it will retry; CLOSED (2) is terminal.
      if (es.readyState !== EventSource.CLOSED) setReconnecting(true);
    };

    return () => es.close();
  }, [serviceId]);

  // Autoscroll to the bottom on new lines, but only if the user is pinned there.
  useLayoutEffect(() => {
    const el = bodyRef.current;
    if (el && pinnedRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [lines]);

  function onScroll() {
    const el = bodyRef.current;
    if (!el) return;
    // Pinned if within ~24px of the bottom.
    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  }

  return (
    <div className={`card elev-sm ${css.panel}`}>
      <div className={css.panelHead}>
        <TerminalWindowIcon size={14} />
        docker logs -f --tail 100 {serviceId}
        <span className={css.panelHeadRight}>
          <span className={`${css.streamDot}${reconnecting ? ` ${css.reconnecting}` : ""}`} />
          {reconnecting ? "reconnecting…" : "streaming"}
        </span>
      </div>
      <div ref={bodyRef} className={`ag-scroll ${css.logBody}`} onScroll={onScroll}>
        {lines.length === 0 ? (
          <div className={css.logEmpty}>Waiting for log output…</div>
        ) : (
          lines.map((l) => (
            <div key={l.key} className={css.logRow}>
              {l.time && <span className={css.logTime}>{l.time}</span>}
              <span className={`${css.logMsg}${l.tone ? ` ${css[l.tone]}` : ""}`}>{l.message}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
