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

interface BlockRow {
  ord: number;
  block_type: string;
  md: string | null;
  caption: string | null;
  // headers + rows are for TABLE blocks — present only when block_type === "table".
  // Using unknown[] here keeps strict TS happy without pulling in a recursive type;
  // we render them via JSON.stringify → then reparse for <table> rendering.
  // {CONTRACT-B 2026-08-05 "headers: jsonb, rows: jsonb"} [CONFIDENCE: CONFIRMED 100%]
  headers: string[] | null;
  rows: unknown[][] | null;
  source_url: string | null;
}

interface FileRow {
  url: string;
  kind: string;
  markdown: string | null;
  // tables is a JSON array of {headers, rows} — same structure as event_content_blocks.tables/rows.
  // {SCHEMA 2026-08-05 "event_media_files.tables jsonb"} [CONFIDENCE: CONFIRMED 100% — psql \d]
  tables: Array<{ headers: string[]; rows: unknown[][] }> | null;
  n_pages: number | null;
  created_at: string;
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
  | { kind: "blocks";     event_id: string; n: number; rows: BlockRow[] }
  | { kind: "files";      event_id: string; n: number; rows: FileRow[] }
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
function BlocksView({ rows }: { rows: BlockRow[] }) {
  return (
    <div>
      {rows.map((b, i) => (
        <div key={i} className="art-block">
          {/* Block header: ordinal + type pill so the user knows what they're looking at */}
          <div className="art-block-head">
            <span className="art-ord">#{b.ord}</span>
            <span className="chip">{b.block_type}</span>
            {b.source_url && (
              <a className="lt-link" href={b.source_url} target="_blank" rel="noreferrer"
                 style={{ fontSize: 11, marginLeft: 6 }}>{b.source_url}</a>
            )}
          </div>
          {/* TABLE block → real <table>; otherwise render md as monospace pre */}
          {b.block_type === "table" && b.headers && b.rows ? (
            <DataTable headers={b.headers} rows={b.rows as unknown[][]} />
          ) : (
            <pre className="src-pre">{b.md ?? b.caption ?? "—"}</pre>
          )}
          {/* Caption supplemental (non-table blocks may still have a caption alongside md) */}
          {b.block_type !== "table" && b.caption && b.md && (
            <div style={{ opacity: 0.6, fontSize: 11, marginTop: 4 }}>↳ {b.caption}</div>
          )}
        </div>
      ))}
    </div>
  );
}

// FILES: per document, a header line "<kind> · <n_pages>p · <url>" then markdown; tables rendered.
// {CONTRACT-B 2026-08-05 "files: header line '<kind> · <n_pages>p · <url>' then its markdown; tables as <table>s"}
// [CONFIDENCE: CONFIRMED 100%]
function FilesView({ rows }: { rows: FileRow[] }) {
  return (
    <div>
      {rows.map((f, i) => (
        <div key={i} className="art-block">
          <div className="art-block-head" style={{ marginBottom: 8 }}>
            <span className="chip">{f.kind}</span>
            {f.n_pages != null && <span style={{ opacity: 0.6, fontSize: 12 }}>{f.n_pages}p</span>}
            {f.url && (
              <a className="lt-link" href={f.url} target="_blank" rel="noreferrer"
                 style={{ fontSize: 11 }}>{f.url}</a>
            )}
          </div>
          {/* Markdown from Docling — the parsed text of the document.
              WHY explicit null/empty check (not falsy guard): empty string is falsy in JS, so
              `f.markdown && <pre>` silently renders nothing when Docling returned "" — a key debug
              state (Docling extracted no text, e.g. scanned/image-only PDF) would be invisible.
              {REVIEW-FINDING-7 2026-08-05 "empty string falsy → FilesView renders nothing with no explanation;
               empty markdown on a parsed file IS the debug signal that Docling failed text extraction"}
              [CONFIDENCE: CONFIRMED 100%] */}
          {f.markdown != null && f.markdown !== ""
            ? <pre className="src-pre">{f.markdown}</pre>
            : <div className="artifacts-empty">No text extracted from this document.</div>
          }
          {/* Embedded tables extracted by Docling — rendered as real HTML tables */}
          {f.tables && f.tables.length > 0 && (
            <div style={{ marginTop: 10 }}>
              {f.tables.map((t, ti) => (
                <div key={ti} style={{ marginBottom: 12 }}>
                  <div style={{ opacity: 0.5, fontSize: 11, marginBottom: 4 }}>Table {ti + 1}</div>
                  <DataTable headers={t.headers} rows={t.rows} />
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

// TRANSCRIPT: "[mm:ss] Speaker: text" per segment; handles NULL start_s gracefully.
// {CONTRACT-B 2026-08-05 "transcript: one line per segment '[mm:ss] <speaker>: <text>' (start_s may be NULL)"}
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
      case "blocks":     return <BlocksView     rows={d.rows} />;
      case "files":      return <FilesView      rows={d.rows} />;
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
