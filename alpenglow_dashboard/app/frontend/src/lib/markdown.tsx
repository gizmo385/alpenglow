/* A small, safe Markdown renderer for Claude's replies on Vikunja cards.
 *
 * Covers what the dispatcher's replies use: paragraphs, "- " bullets
 * (one level of nesting by indent), a line that is all **bold** as a
 * subheading, and inline **bold**, `code` and [links](https://…). It builds
 * React elements, never HTML strings, so nothing in a reply can inject
 * markup; links are kept only for http(s) URLs. */

import type { ReactNode } from "react";

const INLINE = /(\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\((https?:\/\/[^)\s]+)\))/g;

export function inline(text: string, key = ""): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let i = 0;
  for (const m of text.matchAll(INLINE)) {
    const at = m.index ?? 0;
    if (at > last) out.push(text.slice(last, at));
    const k = `${key}-${i++}`;
    if (m[2] !== undefined) out.push(<strong key={k}>{inline(m[2], k)}</strong>);
    else if (m[3] !== undefined) out.push(<code key={k}>{m[3]}</code>);
    else
      out.push(
        <a key={k} href={m[5]} target="_blank" rel="noopener noreferrer">
          {inline(m[4], k)}
        </a>,
      );
    last = at + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

type Block =
  | { kind: "p"; text: string }
  | { kind: "h"; text: string }
  | { kind: "ul"; items: { text: string; sub: string[] }[] };

const BULLET = /^(\s*)[-*•]\s+(.*)$/;
const HEADING = /^(?:#{1,6}\s+(.+)|\*\*([^*]+)\*\*:?)$/;

export function blocks(md: string): Block[] {
  const out: Block[] = [];
  for (const raw of md.replace(/\r/g, "").split("\n")) {
    const line = raw.trimEnd();
    if (!line.trim()) continue;
    const b = BULLET.exec(line);
    if (b) {
      const prev = out[out.length - 1];
      const list = prev?.kind === "ul" ? prev : null;
      if (b[1].length >= 2 && list && list.items.length) {
        list.items[list.items.length - 1].sub.push(b[2]);
      } else if (list) {
        list.items.push({ text: b[2], sub: [] });
      } else {
        out.push({ kind: "ul", items: [{ text: b[2], sub: [] }] });
      }
      continue;
    }
    const h = HEADING.exec(line.trim());
    if (h) {
      out.push({ kind: "h", text: (h[1] ?? h[2]).trim() });
      continue;
    }
    out.push({ kind: "p", text: line.trim() });
  }
  return out;
}

export function Markdown({ text, className }: { text: string; className?: string }) {
  return (
    <div className={className}>
      {blocks(text).map((b, i) => {
        if (b.kind === "h") return <h4 key={i}>{inline(b.text, `h${i}`)}</h4>;
        if (b.kind === "p") return <p key={i}>{inline(b.text, `p${i}`)}</p>;
        return (
          <ul key={i}>
            {b.items.map((it, j) => (
              <li key={j}>
                {inline(it.text, `l${i}-${j}`)}
                {it.sub.length > 0 && (
                  <ul>
                    {it.sub.map((s, k) => (
                      <li key={k}>{inline(s, `l${i}-${j}-${k}`)}</li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        );
      })}
    </div>
  );
}
