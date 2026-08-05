// ArtifactModal — renders the per-kind stage-2 artifact viewer inside the shared .src-modal shell.
// WHY extracted: adding this logic inline in TodayView.tsx would have pushed it past the 1500-line limit
// (rule 4). The modal owns fetch + render; TodayView owns state lifecycle (open/close).
// {CONTRACT-B 2026-08-05 "GET /api/artifact?event_id=<uuid>&kind=<kind> → per-kind payload"}
// [CONFIDENCE: CONFIRMED 100% — API shape is fixed contract written by the task brief]
//
// Caller contract:
//   <ArtifactModal eventId="..." kind="blocks|files|transcript|audio|urls|basic" title="..."
//                  onClose={() => ...} />
// The modal closes when the user clicks the overlay backdrop OR the ✕ button.

import { useEffect, useState } from "react";

// ── shape mirrors /api/artifact response (contract B) ──────────────────────────────────────
// Each "rows" array element is typed per kind; we use a discriminated union on kind for safety.

// DocRow — ONE source url's (md, blocks) pair, as /api/artifact returns it for kind 'blocks' (html pages) and
// kind 'files' (office documents). Both kinds are the same shape now; only the `kind` filter differs server-side.
// {MIGRATION 20260805151246 "EVENT_DOCUMENTS — ONE ROW PER (EVENT, SOURCE URL), HOLDING A PAIR"}
// [CONFIDENCE: CONFIRMED 100% — schema read back from psql after the migration applied.]
interface DocBlock {
  id: number;
  type: string;                     // 'table' | 'figure'
  headers?: string[];
  rows?: unknown[][];
  caption?: string;
  alt?: string;
  src?: string;
}
interface DocRow {
  url: string;
  kind: string;                     // 'html' | 'pdf' | 'pptx' | 'docx' | 'xlsx'
  md: string;                       // prose, with [[TABLE:n]] / [[FIGURE:n]] markers where structures stood
  blocks: DocBlock[] | null;        // what those markers point at, in the same order
  n_pages: number | null;
  n_chars: number;
  n_blocks: number;
}

interface TranscriptRow {
  ord: number;
  speaker: string | null;
  // start_s is NULL for the 90fe56a7 test event — the UI must handle this gracefully.
  // {SCHEMA 2026-08-05 "start_s NULL" + test event 90fe56a7 has speaker Questioner/Answerer but start_s NULL}
  // [CONFIDENCE: CONFIRMED 100% — verified via psql count on event_transcript_segments]
  start_s: number | null;
  end_s: number | null;
  text: string;
  source_url: string | null;
}

interface AudioRow {
  url: string;
  local_path: string | null;
  duration_s: number | null;
  created_at: string;
}

interface UrlRow {
  url: string;
  kind: string;
  status: string;
  created_at: string;
}

interface BasicRow {
  // "basic" kind returns a single pseudo-row with the events.basic_info text.
  // {CONTRACT-B 2026-08-05 "basic -> { kind, event_id, n, rows:[{md: <events.basic_info>}] }"}
  // [CONFIDENCE: CONFIRMED 100% — API contract]
  md: string;
}

// Discriminated union for the /api/artifact response body.
type ArtifactPayload =
  // Both document kinds carry the SAME row shape now — the server filters on `kind`, it does not
  // return a different structure. {ARTIFACT.JS "IF (KIND === \"BLOCKS\" || KIND === \"FILES\")"}
  | { kind: "blocks";     event_id: string; n: number; rows: DocRow[] }
  | { kind: "files";      event_id: string; n: number; rows: DocRow[] }
  | { kind: "transcript"; event_id: string; n: number; rows: TranscriptRow[] }
  | { kind: "audio";      event_id: string; n: number; rows: AudioRow[] }
  | { kind: "urls";       event_id: string; n: number; rows: UrlRow[] }
  | { kind: "basic";      event_id: string; n: number; rows: BasicRow[] };

// ── helpers ─────────────────────────────────────────────────────────────────────────────────

// Format a seconds offset as mm:ss; if null, show "—:—" to avoid crashing on NULL start_s.
// WHY: test event 90fe56a7 has start_s NULL for all transcript segments — the format must not throw.
// {SCHEMA 2026-08-05 "start_s NULL verified for 90fe56a7"} [CONFIDENCE: CONFIRMED 100%]
function fmtSec(s: number | null): string {
  if (s == null || isNaN(s)) return "—:—";
  const m = Math.floor(s / 60);
  const r = Math.floor(s % 60);
  return `${m}:${String(r).padStart(2, "0")}`;
}

// Render a generic {headers, rows} data table as a real <table>.
// WHY a helper: both BlockRow (table blocks) and FileRow (embedded tables) need this.
// {CONTRACT-B 2026-08-05 "if headers/rows are present render an actual <table>"} [CONFIDENCE: CONFIRMED 100%]
function DataTable({ headers, rows }: { headers: string[]; rows: unknown[][] }) {
  return (
    <table className="art-data-table">
      <thead>
        <tr>{headers.map((h, i) => <th key={i}>{h}</th>)}</tr>
      </thead>
      <tbody>
        {rows.map((row, ri) => (
          <tr key={ri}>
            {(row as unknown[]).map((cell, ci) => (
              <td key={ci}>{cell == null ? "" : String(cell)}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ── per-kind renderers ──────────────────────────────────────────────────────────────────────

// BLOCKS: md text per block; TABLE blocks get a real <table> instead of raw JSON.
// {CONTRACT-B 2026-08-05 "blocks/basic: md text, monospace; if headers/rows present render <table>"}
// [CONFIDENCE: CONFIRMED 100%]
// DOCUMENT: one source url's pair, rendered as ONE continuous document — the md with every [[TABLE:n]] marker
// swapped back for the real table from the blocks json.
//
// WHY interleave rather than showing md and json side by side: the pair is a storage decision, not a reading one.
// Splitting prose from structure is what keeps the md chunkable and the tables exact; putting them back together is
// what the reader wants. Storage and presentation get to disagree, and this is the seam where they do.
// {USER 2026-08-05 "ONE MD + PLACEHODLER FOR GRAPHS AND TABELS USING JSON, SO ONE PAIR ... FOR ANY URL LEVEL"}
// [CONFIDENCE: CONFIRMED 100% — direct user directive.]
const PLACEHOLDER_RE = /^\s*\[\[(TABLE|FIGURE):(\d+)\]\]\s*$/;

function DocumentView({ rows }: { rows: DocRow[] }) {
  return (
    <div>
      {rows.map((d, i) => {
        const blocks = d.blocks || [];
        // Split the md on placeholder lines, keeping the markers so each can be swapped for its structure.
        const lines = (d.md || "").split("\n");
        const parts: Array<{ kind: "text"; text: string } | { kind: "block"; b: DocBlock | null; marker: string }> = [];
        let buf: string[] = [];
        const flush = () => { if (buf.length) { parts.push({ kind: "text", text: buf.join("\n") }); buf = []; } };
        for (const ln of lines) {
          const m = PLACEHOLDER_RE.exec(ln);
          if (m) {
            flush();
            const want = m[1].toLowerCase();
            const id = parseInt(m[2], 10);
            // Match on BOTH type and id — a document can hold [[TABLE:1]] and [[FIGURE:1]] at once, so id alone is
            // ambiguous. A marker with no matching block renders as a visible break, never silently vanishes:
            // a missing table the reader cannot see is indistinguishable from a document that never had one.
            const b = blocks.find((x) => (x.type || "").toLowerCase() === want && x.id === id) || null;
            parts.push({ kind: "block", b, marker: ln.trim() });
          } else {
            buf.push(ln);
          }
        }
        flush();
        // A block that no marker referenced would otherwise be invisible — surface it rather than drop it.
        const referenced = new Set(parts.filter((p) => p.kind === "block" && p.b).map((p) => `${(p as {b: DocBlock}).b.type}:${(p as {b: DocBlock}).b.id}`));
        const orphans = blocks.filter((b) => !referenced.has(`${b.type}:${b.id}`));

        return (
          <div key={i} className="art-block">
            <div className="art-block-head" style={{ marginBottom: 8 }}>
              <span className="chip">{d.kind}</span>
              {d.n_pages != null && <span style={{ opacity: 0.6, fontSize: 12 }}>{d.n_pages}p</span>}
              <span style={{ opacity: 0.6, fontSize: 12 }}>
                {(d.n_chars ?? 0).toLocaleString()} chars · {d.n_blocks ?? 0} structured
              </span>
              {d.url && (
                <a className="lt-link" href={d.url} target="_blank" rel="noreferrer"
                   style={{ fontSize: 11 }}>{d.url}</a>
              )}
            </div>
            {/* Explicit empty check, not a falsy guard: an empty md is a real and important debug state (the
                extractor read the source and got nothing) and must not render as a blank gap. */}
            {!(d.md || "").trim() ? (
              <div className="artifacts-empty">empty — the extractor produced no text for this source</div>
            ) : parts.map((p, j) =>
              p.kind === "text" ? (
                <pre key={j} className="src-pre">{p.text}</pre>
              ) : p.b && p.b.type === "table" ? (
                <DataTable key={j} headers={p.b.headers || []} rows={(p.b.rows as unknown[][]) || []} />
              ) : p.b ? (
                <div key={j} className="art-block-head" style={{ opacity: 0.7, fontSize: 12 }}>
                  🖼 {p.b.caption || p.b.alt || p.b.src || "figure"}
                </div>
              ) : (
                <div key={j} style={{ color: "#f87171", fontSize: 12, padding: "4px 0" }}>
                  {p.marker} — no matching block in the structured json
                </div>
              ))}
            {orphans.length > 0 && (
              <div style={{ marginTop: 10 }}>
                <div style={{ color: "#f87171", fontSize: 12 }}>
                  {orphans.length} structured block(s) with no placeholder in the md:
                </div>
                {orphans.map((b, j) => (
                  <DataTable key={j} headers={b.headers || []} rows={(b.rows as unknown[][]) || []} />
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// [CONFIDENCE: CONFIRMED 100%]
function TranscriptView({ rows }: { rows: TranscriptRow[] }) {
  return (
    <div style={{ fontFamily: "var(--mono)", fontSize: 12.5, lineHeight: 1.7 }}>
      {rows.map((s, i) => (
        <div key={i} style={{ display: "flex", gap: 10, padding: "2px 0" }}>
          {/* Timestamp — right-padded so columns align even with NULL */}
          <span style={{ color: "var(--text-muted)", minWidth: 44, flexShrink: 0 }}>
            [{fmtSec(s.start_s)}]
          </span>
          {/* Speaker badge when present */}
          {s.speaker && (
            <span style={{ color: "var(--accent-ink)", fontWeight: 600, minWidth: 90, flexShrink: 0 }}>
              {s.speaker}:
            </span>
          )}
          <span style={{ color: "var(--text-secondary)", wordBreak: "break-word" }}>{s.text}</span>
        </div>
      ))}
    </div>
  );
}

// AUDIO: url + local_path + duration — minimal view because event_audio has 0 rows today.
// {CONTRACT-B 2026-08-05 "audio: url + local_path + duration"}
// [CONFIDENCE: CONFIRMED 100%]
function AudioView({ rows }: { rows: AudioRow[] }) {
  return (
    <div>
      {rows.map((a, i) => (
        <div key={i} className="art-block">
          {a.url && <div><a className="lt-link" href={a.url} target="_blank" rel="noreferrer">{a.url}</a></div>}
          {a.local_path && <div style={{ opacity: 0.7, fontSize: 12, marginTop: 4 }}>local: {a.local_path}</div>}
          {a.duration_s != null && (
            <div style={{ opacity: 0.7, fontSize: 12, marginTop: 4 }}>duration: {fmtSec(a.duration_s)}</div>
          )}
        </div>
      ))}
    </div>
  );
}

// URLS: the per-url ledger — a real table with failed rows visually marked.
// WHY visual mark on failed: the ledger is the ONLY place per-url failure is visible; a failed row must
// pop out immediately so an operator knows exactly which URL the media agent choked on.
// {TASK-BRIEF 2026-08-05 "urls: real table of url | kind | status; failed rows visually marked"}
// [CONFIDENCE: CONFIRMED 100% — contract D: "give the button a distinct look when failed > 0"]
// WHY created_at column added (finding 9): the timestamp tells the operator WHEN a failure happened,
// which is necessary for correlating with logs; the field is fetched by artifact.js but was silently dropped.
// {REVIEW-FINDING-9 2026-08-05 "UrlsView omits created_at column even though artifact.js selects it;
//  timestamp is essential for correlating failures with logs"}
// [CONFIDENCE: CONFIRMED 100%]
function UrlsView({ rows }: { rows: UrlRow[] }) {
  return (
    <table className="art-data-table">
      <thead>
        <tr><th>URL</th><th>Kind</th><th>Status</th><th>Processed</th></tr>
      </thead>
      <tbody>
        {rows.map((u, i) => {
          // Classify status for colour coding: failed is the critical debug signal.
          const isFailed = u.status === "failed";
          const isSkipped = u.status === "skipped";
          // Format created_at as a short local time for quick correlation; fall back to raw string.
          const processed = u.created_at
            ? new Date(u.created_at).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
            : "—";
          return (
            <tr key={i} className={isFailed ? "art-row-failed" : undefined}>
              <td style={{ wordBreak: "break-all" }}>
                <a className="lt-link" href={u.url} target="_blank" rel="noreferrer">{u.url}</a>
              </td>
              <td><span className="chip">{u.kind}</span></td>
              <td style={{ color: isFailed ? "#f87171" : isSkipped ? "var(--text-muted)" : "#6ee7a8",
                           fontWeight: isFailed ? 600 : undefined }}>
                {u.status}
              </td>
              <td style={{ opacity: 0.7, whiteSpace: "nowrap" }}>{processed}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

// BASIC: events.basic_info as markdown monospace — same treatment as blocks.
// {CONTRACT-B 2026-08-05 "basic → {md: <events.basic_info>}"} [CONFIDENCE: CONFIRMED 100%]
function BasicView({ rows }: { rows: BasicRow[] }) {
  const first = rows[0];
  if (!first) return <div className="artifacts-empty">No basic info stored.</div>;
  return <pre className="src-pre">{first.md}</pre>;
}

// ── main component ──────────────────────────────────────────────────────────────────────────

interface ArtifactModalProps {
  eventId: string;
  // "kind" is the /api/artifact kind query param; also used as the modal title suffix.
  kind: string;
  // "title" is the event title shown in the modal header.
  title: string;
  onClose: () => void;
}

export default function ArtifactModal({ eventId, kind, title, onClose }: ArtifactModalProps) {
  // Fetch state: "loading" string = in-flight; ArtifactPayload = done; other string = error.
  // WHY no null in the union: initial value is "loading" (string) and no setState(null) call exists;
  // null was a commented-intended "not yet started" state that was never wired up, creating a dead branch.
  // {REVIEW-FINDING-8 2026-08-05 "null branch in `if (state === 'loading' || state === null)` is dead code —
  //  state is never null; initial value is string 'loading', all setters pass string or ArtifactPayload"}
  // [CONFIDENCE: CONFIRMED 100%]
  const [state, setState] = useState<ArtifactPayload | string>("loading");

  // Kick the fetch on mount; re-fetch if eventId or kind changes (the user can swap buttons).
  // WHY useEffect: the fetch is a side-effect; React strict-mode double-fires but that's harmless
  // because we always replace state and never accumulate.
  // {CONTRACT-B 2026-08-05 "GET /api/artifact?event_id=<uuid>&kind=<kind>"} [CONFIDENCE: CONFIRMED 100%]
  useEffect(() => {
    setState("loading");
    fetch(`/api/artifact?event_id=${encodeURIComponent(eventId)}&kind=${encodeURIComponent(kind)}`)
      .then((r) => r.json())
      .then((d: ArtifactPayload) => setState(d))
      .catch((err: unknown) => setState(String(err)));
  }, [eventId, kind]);

  // Render the right sub-component based on the returned kind.
  // MUST switch on d.kind (not the prop) so the discriminated union narrows correctly.
  function renderBody() {
    if (state === "loading") return <div style={{ padding: 20 }}>Loading…</div>;
    if (typeof state === "string") {
      // state is an error message string after a failed fetch.
      return <div className="artifacts-empty" style={{ color: "#f87171" }}>Error: {state}</div>;
    }
    const d = state;
    if (d.n === 0 || d.rows.length === 0) {
      return <div className="artifacts-empty">No {d.kind} artifacts found for this event.</div>;
    }
    // Discriminated switch — TypeScript narrows d.rows type per branch.
    switch (d.kind) {
      case "blocks":     return <DocumentView   rows={d.rows} />;
      case "files":      return <DocumentView   rows={d.rows} />;
      case "transcript": return <TranscriptView rows={d.rows} />;
      case "audio":      return <AudioView      rows={d.rows} />;
      case "urls":       return <UrlsView       rows={d.rows} />;
      case "basic":      return <BasicView      rows={d.rows} />;
    }
  }

  // Row count badge shown in the modal header alongside the kind label.
  const n = typeof state === "object" && state !== null && !Array.isArray(state)
    ? (state as ArtifactPayload).n
    : null;

  return (
    // Overlay: click outside modal → close.
    // .src-overlay / .src-modal / .src-head / .src-body reuse existing CSS — no new classes needed.
    // {STYLES 2026-08-05 ".src-overlay L423, .src-modal L428, .src-head L435, .src-body L444"}
    // [CONFIDENCE: CONFIRMED 100% — read from styles.css same session]
    <div className="src-overlay" onClick={onClose}>
      <div className="src-modal" onClick={(ev) => ev.stopPropagation()}>
        <div className="src-head">
          <div className="src-title">
            {/* Kind tag + count + event title: e.g. "blocks (12) — Apple Q4 2025 Earnings Call" */}
            <span className="chip" style={{ marginRight: 8 }}>{kind}</span>
            {n != null && <span style={{ opacity: 0.6, fontSize: 12, marginRight: 8 }}>({n})</span>}
            {title}
          </div>
          <button className="src-close" onClick={onClose}>✕</button>
        </div>
        <div className="src-body">
          {renderBody()}
        </div>
      </div>
    </div>
  );
}
