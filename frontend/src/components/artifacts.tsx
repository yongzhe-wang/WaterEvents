// Shared artifact affordances — the per-event inventory type, the button row, and the hook that loads an inventory
// for a page's worth of event ids. Lives here rather than inside one view because BOTH "today" pages need it:
// Today Events shows what stage-1 just discovered, Today Media shows what stage-2 produced, and the artifact buttons
// are the same instrument on both.
// {USER 2026-08-05 "let's keep two pages one is today events and one is today media ... i want to autall inspect the medai runs"}
// [CONFIDENCE: CONFIRMED 100% — direct user directive to split the pages; the buttons predate the split and are reused verbatim.]
import { useEffect, useState } from "react";

// ArtifactInv — the per-event inventory returned by /api/artifacts (batch endpoint, contract A).
// WHY a separate interface here (not just in ArtifactModal): TodayView owns the inventory state and
// decides which buttons to render; the modal only receives (eventId, kind) and fetches its own content.
// Keeping the inventory type here makes the button-rendering logic self-contained.
// {CONTRACT-A 2026-08-05 "GET /api/artifacts?event_ids=... → { inventory: { <event_id>: { blocks, files, segments, audio, urls, basic_info } } }"}
// [CONFIDENCE: CONFIRMED 100% — API contract is fixed, written by the task brief]
export interface FileKindCount { kind: string; n_pages: number | null; }
export interface UrlTally { done: number; failed: number; skipped: number; }
export interface EventArtInv {
  blocks: number;
  files: FileKindCount[];
  segments: number;
  audio: number;
  urls: UrlTally;
  basic_info: boolean;
}
// Map from event_id → its inventory, false = confirmed-empty (API omitted; no artifacts), undefined = not yet loaded.
// WHY the false sentinel: after the inventory resolves, missing event_id means CONFIRMED zero artifacts (the API
// contract omits zero-artifact events); we must not render the loading "·" indefinitely for those rows.
// {REVIEW-FINDING-5 2026-08-05 "loading sentinel (null) and confirmed-absent state (undefined → null) are
//  indistinguishable after inventory loads — zero-artifact events permanently show loading dot instead of —"}
// [CONFIDENCE: CONFIRMED 100% — review finding independently verified]
export type ArtInvMap = Record<string, EventArtInv | false>;

// ArtifactButtons — renders the row of artifact-kind buttons for ONE event.
// WHY a separate function: the per-event button logic is >30 lines; extracting it keeps the table's
// map() callback readable and allows early-return for the "no inventory yet" case.
// {CONTRACT-C 2026-08-05 "per row, render one small button per artifact type THAT EXISTS; labelled with kind + count"}
// [CONFIDENCE: CONFIRMED 100% — contract is fixed]
// inv === undefined = not yet loaded (show "·"); inv === false = confirmed empty (show "—"); EventArtInv = has data.
// WHY the three-way sentinel: after inventory resolves, zero-artifact events are OMITTED by the API —
// writing false for omitted ids lets us distinguish confirmed-empty from still-loading.
// {REVIEW-FINDING-5 2026-08-05 "null/undefined conflation caused zero-artifact rows to permanently display loading dot"}
// [CONFIDENCE: CONFIRMED 100%]
export function ArtifactButtons({ inv, onOpen }: { inv: EventArtInv | false | undefined; onOpen: (kind: string) => void }) {
  // undefined = inventory fetch not yet complete — show subtle placeholder so column width doesn't jump.
  // WHY "·" not nothing: an empty cell would cause the column to collapse; a dim dot signals "loading".
  if (inv === undefined) return <span style={{ opacity: 0.25, fontSize: 11 }}>·</span>;

  // false = inventory resolved but this event has zero artifacts — show confirmed-empty dash.
  if (inv === false) return <span style={{ opacity: 0.25, fontSize: 11 }}>—</span>;

  // Collect the buttons to render — ORDER matters for visual consistency across rows.
  // Each entry carries an explicit count so label formatting never requires re-parsing the string.
  // WHY count field: if fk.kind contains a space, split(" ")[1]+parseInt trick returns NaN.
  // {REVIEW-FINDING-6 2026-08-05 "existing.label.split(' ')[1] → parseInt → NaN when kind contains space"}
  // [CONFIDENCE: CONFIRMED 100%]
  const buttons: { label: string; kind: string; warn: boolean; count: number }[] = [];

  // BLOCKS (md): text content blocks extracted from the event page.
  if (inv.blocks > 0) buttons.push({ label: `md ${inv.blocks}`, kind: "blocks", warn: false, count: inv.blocks });

  // FILES: one button PER DISTINCT file kind (pdf / pptx / xlsx / docx / …) with count of that kind.
  // WHY per-kind: the user wants to know specifically if a PDF was parsed vs a PPTX — different quality.
  // {CONTRACT-C 2026-08-05 "one button per DISTINCT file kind (pdf/pptx/xlsx/docx/...) with count of that kind"}
  // [CONFIDENCE: CONFIRMED 100%]
  for (const fk of inv.files) {
    // Accumulate counts when the same kind appears multiple times (edge case; normally one row per file).
    const existing = buttons.find((b) => b.kind === `files:${fk.kind}`);
    if (existing) {
      // Increment the stored count and reformat the label — avoids split+parseInt fragility.
      existing.count++;
      existing.label = `${fk.kind} ${existing.count}`;
    } else {
      buttons.push({ label: `${fk.kind} 1`, kind: `files:${fk.kind}`, warn: false, count: 1 });
    }
  }

  // TRANSCRIPT (text segments from Whisper).
  if (inv.segments > 0) buttons.push({ label: `text ${inv.segments}`, kind: "transcript", warn: false, count: inv.segments });

  // AUDIO.
  if (inv.audio > 0) buttons.push({ label: `audio ${inv.audio}`, kind: "audio", warn: false, count: inv.audio });

  // URLS (the per-url ledger): renders whenever the ledger has ANY row, even if all failed.
  // Label: "<done>/<failed>" so failure is visible WITHOUT opening the modal.
  // warn=true (distinct visual) when failed > 0 — this IS the primary debug signal.
  // {CONTRACT-C 2026-08-05 "urls renders whenever ledger has any row; '<done>/<failed>' label; distinct when failed>0"}
  // [CONFIDENCE: CONFIRMED 100%]
  const totalUrls = (inv.urls.done ?? 0) + (inv.urls.failed ?? 0) + (inv.urls.skipped ?? 0);
  if (totalUrls > 0) {
    const warn = (inv.urls.failed ?? 0) > 0;
    const urlCount = totalUrls;
    buttons.push({ label: `urls ${inv.urls.done}/${inv.urls.failed}`, kind: "urls", warn, count: urlCount });
  }

  // BASIC INFO (events.basic_info non-empty).
  if (inv.basic_info) buttons.push({ label: "info", kind: "basic", warn: false, count: 1 });

  // If all counts are zero AND no url ledger rows → dim "—" so the cell is not blank.
  // (This path is only reached when inv is a real EventArtInv object with all zeros — unusual but possible.)
  if (buttons.length === 0) return <span style={{ opacity: 0.25, fontSize: 11 }}>—</span>;

  // Resolve the actual "kind" sent to the API: files buttons encode "files:<subkind>" so we can
  // distinguish "pdf" from "pptx" in the button row; strip the prefix before calling /api/artifact.
  // {CONTRACT-B 2026-08-05 "KIND IS ONE OF: BLOCKS | FILES | TRANSCRIPT | AUDIO | URLS | BASIC — 'FILES:PDF' IS NOT A VALID KIND"}
  // [CONFIDENCE: CONFIRMED 100% — VALID_KINDS set in artifact.js defines the accepted values]
  // {REVIEW-FINDING-13 2026-08-05 "resolveKind header missing dual-bracket discipline per rule 17"}
  function resolveKind(rawKind: string): string {
    // "files:pdf" → kind="files" for the API; the modal currently shows all file rows regardless of subkind.
    // WHY: the API contract uses kind="files" (not kind="files:pdf"); filtering by subkind is future work.
    return rawKind.startsWith("files:") ? "files" : rawKind;
  }

  return (
    <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
      {buttons.map(({ label, kind: rawKind, warn }) => (
        <button
          key={rawKind}
          // .art-btn: the base artifact button — same monospace pill shape as .src-btn but smaller.
          // .art-btn.bad: red-tinted border + text when warn=true (failed urls visible at a glance).
          // {STYLES 2026-08-05 "art-btn / art-btn.bad — specs sent to CSS agent"}
          // [CONFIDENCE: CONFIRMED 100% — new classes, CSS agent will implement]
          className={`art-btn${warn ? " bad" : ""}`}
          onClick={() => onOpen(resolveKind(rawKind))}
          title={warn ? "failed URLs in ledger" : label}
        >
          {label}
        </button>
      ))}
    </div>
  );
}


/**
 * Load the artifact inventory for a page's worth of event ids.
 *
 * WHY a hook rather than inline effects in each view: both pages need the exact same three-state behaviour, and the
 * subtle part is the THIRD state. The API omits events that have no artifacts, so an id missing from the response
 * means "confirmed empty", not "still loading" — writing `false` for every id we asked about but did not get back is
 * what keeps zero-artifact rows from showing a loading dot forever.
 * {REVIEW-FINDING-5 2026-08-05 "LOADING SENTINEL (NULL) AND CONFIRMED-ABSENT STATE (UNDEFINED -> NULL) ARE
 *  INDISTINGUISHABLE AFTER INVENTORY LOADS — ZERO-ARTIFACT EVENTS PERMANENTLY SHOW LOADING DOT INSTEAD OF —"}
 * [CONFIDENCE: CONFIRMED 100% — review finding, independently verified before this hook was written.]
 *
 * Deliberately does NOT block the table's first paint: the caller renders rows as soon as its own data arrives and the
 * buttons fill in when this resolves. An inventory request that is slow or fails must never hold back the page.
 */
export function useArtifactInventory(ids: string[]): ArtInvMap {
  const [inv, setInv] = useState<ArtInvMap>({});
  // Join into a primitive so the effect re-runs when the SET of ids changes, not on every parent render — a fresh
  // array literal with identical contents is a new reference and would otherwise poll far faster than intended.
  const key = ids.join(",");
  useEffect(() => {
    if (!key) return;
    let cancelled = false;                                  // a page switched away must not write into a dead component
    fetch(`/api/artifacts?event_ids=${encodeURIComponent(key)}`)
      .then((r) => r.json())
      .then((d) => {
        if (cancelled) return;
        const got = (d && d.inventory) || {};
        const next: ArtInvMap = {};
        // Every id we ASKED about gets an entry: its inventory, or `false` meaning confirmed-empty.
        for (const id of key.split(",")) next[id] = got[id] || false;
        setInv(next);
      })
      .catch(() => { /* leave prior state; the buttons simply stay as they were rather than flashing empty */ });
    return () => { cancelled = true; };
  }, [key]);
  return inv;
}
