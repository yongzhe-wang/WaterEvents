// Frontend mirrors of the backend JSON shapes (dashboard/backend/app.py).
// Read-only viewer — these are display models, never written back.

// Top-line counts for the header (GET /api/stats).
export interface Stats {
  companies: number;
  events: number;
  artifacts: number;
  extracted: number; // files whose text/transcript was successfully pulled
}

// One row in the company list (GET /api/companies). `uncatalogued` = has events
// but no curated company-metadata row (e.g. LLY, MU).
export interface Company {
  ticker: string;
  company_name: string;
  sector: string | null;
  industry: string | null;
  exchange: string | null;
  market_cap: number | null;
  ir_url: string | null;
  ir_url_confidence: string | null;
  uncatalogued: boolean;
  event_count: number;
  artifact_count: number;
  crawl_ms: number | null;   // per-company crawl wall-clock (metadata from ir_method_registry)
  crawl_dead: string | null; // incomplete_reason when the crawl DIED mid-run (fc_credits_exhausted…), else null
  also_tickers?: string[];   // dual-class twins merged into this one (e.g. GOOGL covers GOOG)
}

// One extracted artifact under an event — one of the four things the pipeline
// captures: slides / transcript / audio (the detail TEXT lives on the event).
export interface Artifact {
  id: number;
  event_id: number;
  kind: string; // 'slides' | 'transcript' | 'audio'
  source_url: string | null;
  local_path: string | null; // file under data/events/<TICKER>/
  content_preview: string; // first N chars of the extracted text / whisper transcript
  content_chars: number; // true length of the full extracted text
  content_truncated: boolean; // preview is shorter than the full text
  content_format: string | null; // 'markdown' | 'text'
  status: string; // extracted | transcribed | failed | no_url
  bytes: number | null;
  latency_ms: number | null;
}

// One Stage-2 event with its detail text + artifacts + availability flags
// (GET /api/companies/{ticker}). The four things: event_text (detail) + the
// slides / transcript / audio artifacts.
export interface IREvent {
  id: number;
  ticker: string;
  event_title: string;
  event_date: string;
  event_type: string;
  event_url: string | null;
  event_text: string; // scraped event-page detail text (preview)
  event_text_chars: number;
  event_text_truncated: boolean;
  slides_url: string | null;
  transcript_url: string | null;
  audio_url: string | null;
  // Filing (10-Q/10-K) + financial-statements source URLs — some companies (Apple)
  // publish these instead of a shareholder deck, so they get their own pills too.
  filing_url: string | null;
  financials_url: string | null;
  status: string;
  latency_ms: number | null;
  artifacts: Artifact[];
  has_detail: boolean;
  has_slides: boolean;
  has_transcript: boolean;
  has_audio: boolean;
}

// Full company-detail payload (GET /api/companies/{ticker}).
// WatchPage / `watch` field removed — link bank (ir_watch_urls) is retired; the pipeline monitors
// companies.ir_url directly, so there are no per-company source pages. {USER 2026-07-08 "link bank 全废"}.
export interface CompanyDetail {
  company: Record<string, unknown> & { ticker: string; company_name?: string };
  events: IREvent[];
  // ir_method_registry keyed by kind ({ir_crawl: {…}}) — complete/incomplete_reason = dead-crawl marker.
  methods?: Record<string, { method?: string; latency_ms?: number; complete?: boolean; incomplete_reason?: string | null }>;
}
