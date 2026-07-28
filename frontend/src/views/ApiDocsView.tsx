// API docs — the public read-only surface on :8090, documented for whoever is integrating against it (Vincent's side,
// and anyone we hand the base URL to). {USER 2026-07-28 "first create a doc page on the web app for this api and how to
// use it and what it return"}.
//
// 用一句话讲完: 一个静态说明页 + 一个实时 Try it 面板 —— 说明页讲参数/返回字段/错误码, Try it 直接打线上那台
// :8090, 所以文档写的和服务实际返回的对不上时, 页面自己就会露馅, 不需要有人去比对。
//
// WHY the live panel and not just a pasted sample: a docs page whose examples are hand-copied drifts the moment the
// endpoint changes, and nobody notices until an integrator hits the mismatch. Calling the real service on the page
// makes the sample self-refreshing and doubles as a deploy check — before the migration is applied the panel shows the
// 503, which is exactly the information a reader needs.
// {backend/api_service/main.py "Access-Control-Allow-Origin: *"} — the CORS header is what lets this page, served from
// :8080, call :8090 from the browser at all. [CONFIDENCE: CONFIRMED 100% — header set on both read endpoints].
import { useState } from "react";

// The public service. Deliberately NOT this dashboard's own origin — the docs describe :8090 (public, unauthenticated)
// while this page is served from :8080 (password-gated), and every curl line below is copied by someone who is NOT
// behind that password. Conflating the two is the exact mistake this page exists to prevent, so the host is written
// out in full rather than derived from window.location.
const API_BASE = "http://35.254.161.69:8090";

const BUCKETS = ["minute", "hour", "day"] as const;
type Bucket = (typeof BUCKETS)[number];

// Copy-to-clipboard code block. The copy button is the whole reason integrators can lift a curl line without
// re-typing a long URL; `navigator.clipboard` is https/localhost-only, so the catch falls back to leaving the text
// selectable rather than pretending it worked.
function Code({ children }: { children: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="docs-code">
      <button className="docs-copy" onClick={() => {
        navigator.clipboard?.writeText(children).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        }).catch(() => undefined);
      }}>{copied ? "copied" : "copy"}</button>
      <pre>{children}</pre>
    </div>
  );
}

// Live caller for GET /today/events. Holds the raw JSON so the reader sees the ACTUAL shape, including the error
// shapes — a 503 here means the RPC is not deployed yet, a 429 means the rate limit tripped, and both are documented
// below, so seeing them is instructive rather than a broken page.
function TryIt() {
  const [bucket, setBucket] = useState<Bucket>("hour");
  const [limit, setLimit] = useState("5");
  const [out, setOut] = useState<string>("");
  const [status, setStatus] = useState<string>("");
  const [busy, setBusy] = useState(false);

  const url = `${API_BASE}/today/events?bucket=${bucket}&limit=${limit}`;
  const run = async () => {
    setBusy(true);
    setStatus("");
    setOut("");
    try {
      const t0 = Date.now();
      const r = await fetch(url);
      const body = await r.text();
      setStatus(`HTTP ${r.status} · ${Date.now() - t0}ms`);
      // Pretty-print when it parses; fall through to the raw body so a non-JSON error page is still visible.
      try { setOut(JSON.stringify(JSON.parse(body), null, 2)); } catch { setOut(body); }
    } catch (e) {
      // A network-level failure here is almost always "the service is not running" or a blocked mixed-content
      // request, neither of which produces an HTTP status — say so instead of showing an empty box.
      setStatus("network error — service unreachable from this browser");
      setOut(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="docs-ep-detail">
      <div className="docs-ep-sig">
        <span className="docs-method docs-method-get">GET</span>
        <code>Try it</code>
      </div>
      <p className="docs-p docs-muted" style={{ marginBottom: 10 }}>
        Calls the live service from your browser. No credentials are sent — this is the same request any client makes.
      </p>
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", marginBottom: 10 }}>
        <label style={{ fontSize: 13, color: "var(--text-muted)" }}>bucket</label>
        <select value={bucket} onChange={(e) => setBucket(e.target.value as Bucket)}
                style={{ background: "var(--glass-2)", color: "var(--text)", border: "1px solid var(--glass-border-soft)", borderRadius: 6, padding: "4px 8px", fontFamily: "var(--mono)", fontSize: 12 }}>
          {BUCKETS.map((b) => <option key={b} value={b}>{b}</option>)}
        </select>
        <label style={{ fontSize: 13, color: "var(--text-muted)" }}>limit</label>
        <input value={limit} onChange={(e) => setLimit(e.target.value)} size={4}
               style={{ background: "var(--glass-2)", color: "var(--text)", border: "1px solid var(--glass-border-soft)", borderRadius: 6, padding: "4px 8px", fontFamily: "var(--mono)", fontSize: 12, width: 60 }} />
        <button className="docs-copy" style={{ position: "static" }} disabled={busy} onClick={run}>
          {busy ? "calling…" : "send"}
        </button>
        {status && <span style={{ fontFamily: "var(--mono)", fontSize: 11.5, color: "var(--text-muted)" }}>{status}</span>}
      </div>
      <div className="docs-code"><pre style={{ maxHeight: 320 }}>{out || `# ${url}\n# press send`}</pre></div>
    </div>
  );
}

export default function ApiDocsView() {
  return (
    <div className="docs">
      <div className="docs-hero">
        <h2>WaterEvents public API</h2>
        <p className="docs-lead">
          A <strong>read-only, unauthenticated</strong> feed of investor-relations events as we discover them.
          Two endpoints: <span className="docs-ic">/today/pulse</span> tells you <strong>how many</strong> events
          landed in the last minute, hour and day; <span className="docs-ic">/today/events</span> gives you
          <strong> the events themselves</strong>. Both use the same definition, so the counter and the feed always agree.
        </p>
      </div>

      <div className="docs-cards" style={{ marginBottom: 20 }}>
        <div className="docs-card">
          <div className="docs-card-h">No key required</div>
          <p>
            Plain HTTP GET, <strong>CORS open</strong>, nothing to sign. Rate limited to <strong>60 requests per
            minute per IP</strong>; responses are cached for 10 seconds, so polling faster than that returns the same
            payload.
          </p>
        </div>
        <div className="docs-card">
          <div className="docs-card-h">Bounded by time, not by page</div>
          <p>
            The widest window is <strong>one day</strong>. There is no <span className="docs-ic">offset</span>, so
            these endpoints expose recent discoveries only — they are not a route into the full archive. Ask for a
            bigger <span className="docs-ic">limit</span> than 500 and it is silently clamped.
          </p>
        </div>
      </div>

      <div className="section-label">Endpoints<span className="rule" /></div>
      <table className="docs-ep-table">
        <thead>
          <tr><th style={{ width: 62 }}>Method</th><th style={{ width: 190 }}>Path</th><th>What it returns</th></tr>
        </thead>
        <tbody>
          <tr>
            <td><span className="docs-method docs-method-get">GET</span></td>
            <td className="docs-ep-path">/today/events</td>
            <td className="docs-ep-desc">The events discovered in the last minute / hour / day, with their metadata.</td>
          </tr>
          <tr>
            <td><span className="docs-method docs-method-get">GET</span></td>
            <td className="docs-ep-path">/today/pulse</td>
            <td className="docs-ep-desc">Counts only, for the same three windows. Cheap enough to poll continuously.</td>
          </tr>
          <tr>
            <td><span className="docs-method docs-method-get">GET</span></td>
            <td className="docs-ep-path">/health</td>
            <td className="docs-ep-desc">Liveness. Touches no database, so probing it costs nothing.</td>
          </tr>
        </tbody>
      </table>
      <p className="docs-p docs-muted" style={{ marginTop: 10 }}>
        Base URL <span className="docs-ic">{API_BASE}</span>
      </p>

      <div className="docs-h3">GET /today/events</div>
      <Code>{`curl "${API_BASE}/today/events?bucket=hour&limit=100"`}</Code>

      <div className="docs-resp-label">Query parameters</div>
      <table className="docs-ep-table">
        <thead>
          <tr><th style={{ width: 90 }}>Name</th><th style={{ width: 130 }}>Default</th><th>Meaning</th></tr>
        </thead>
        <tbody>
          <tr>
            <td className="docs-ep-path">bucket</td>
            <td className="docs-ep-path">hour</td>
            <td className="docs-ep-desc">
              How far back to look: <span className="docs-ic">minute</span>, <span className="docs-ic">hour</span> or
              <span className="docs-ic"> day</span>. Anything else returns 400 with the allowed list.
            </td>
          </tr>
          <tr>
            <td className="docs-ep-path">limit</td>
            <td className="docs-ep-path">100</td>
            <td className="docs-ep-desc">
              Max events returned, 1–500. Values above 500 are clamped rather than rejected; check
              <span className="docs-ic"> truncated</span> to know whether you saw everything.
            </td>
          </tr>
          <tr>
            <td className="docs-ep-path">tz</td>
            <td className="docs-ep-path">UTC</td>
            <td className="docs-ep-desc">
              Which calendar decides what "current quarter / month / year" means — see <em>How an event qualifies</em> below.
              It does <strong>not</strong> shift the lookback window, and it does not change{" "}
              <span className="docs-ic">discovered</span>, which is always UTC. Allowed:{" "}
              <span className="docs-ic">UTC</span>, <span className="docs-ic">America/New_York</span>,{" "}
              <span className="docs-ic">America/Los_Angeles</span>, <span className="docs-ic">America/Chicago</span>,{" "}
              <span className="docs-ic">Europe/London</span>, <span className="docs-ic">Europe/Paris</span>,{" "}
              <span className="docs-ic">Asia/Tokyo</span>, <span className="docs-ic">Asia/Shanghai</span>,{" "}
              <span className="docs-ic">Asia/Hong_Kong</span>.
            </td>
          </tr>
        </tbody>
      </table>

      <div className="docs-resp-label">Response</div>
      <Code>{`{
  "now":     "2026-07-28T07:59:15",   // service clock, in the requested tz
  "tz":      "UTC",
  "bucket":  "hour",
  "anchors": { "today": "2026-07-28", "month": "2026-07",
               "quarter": "2026-Q3", "last_quarter": "2026-Q2", "year": "2026" },

  "count":     19,      // events matching in this window  (== /today/pulse buckets.hour)
  "fetched":   1145,    // rows we crawled in this window, before the "is it current" filter
  "limit":     100,     // the cap actually applied
  "returned":  19,      // how many are in events[] below
  "truncated": false,   // true when count > limit

  "events": [
    {
      "id":         "d1e6bc2d-21a1-4693-bb1d-50f6ac9f5de7",
      "ticker":     "9697.T",
      "title":      "Q1 FY2026 Financial Results Press Conference",
      "event_date": "2026-07-28",
      "event_type": "press_release",
      "url":        "https://www.youtube.com/user/CapcomIR",
      "media_urls": ["https://www.youtube.com/user/CapcomIR"],
      "source_url": "https://www.capcom.co.jp/ir/english/investor.html",
      "discovered": "2026-07-28T07:52:40Z"
    }
  ]
}`}</Code>

      <div className="docs-resp-label">Field reference</div>
      <table className="docs-ep-table">
        <thead><tr><th style={{ width: 130 }}>Field</th><th>Meaning</th></tr></thead>
        <tbody>
          <tr><td className="docs-ep-path">id</td><td className="docs-ep-desc">Stable UUID. Safe to use for de-duplication across polls.</td></tr>
          <tr><td className="docs-ep-path">ticker</td><td className="docs-ep-desc">The company's symbol, e.g. <span className="docs-ic">AAPL</span>, <span className="docs-ic">9697.T</span>. <span className="docs-ic">null</span> if the company row is missing.</td></tr>
          <tr><td className="docs-ep-path">title</td><td className="docs-ep-desc">Headline as published by the company.</td></tr>
          <tr><td className="docs-ep-path">event_date</td><td className="docs-ep-desc">The period the event is <em>about</em>. A string, not a date — about three quarters are <span className="docs-ic">YYYY-MM-DD</span>, the rest are coarser (<span className="docs-ic">2026-Q2</span>, <span className="docs-ic">2026-07</span>, <span className="docs-ic">2026</span>, <span className="docs-ic">2026-FY</span>, <span className="docs-ic">2026-H1</span>). Parse defensively.</td></tr>
          <tr><td className="docs-ep-path">event_type</td><td className="docs-ep-desc">e.g. <span className="docs-ic">press_release</span>, <span className="docs-ic">conference</span>, <span className="docs-ic">earnings_call</span>.</td></tr>
          <tr><td className="docs-ep-path">url</td><td className="docs-ep-desc">The first usable link for the event — the one to open. <span className="docs-ic">null</span> when we found no link.</td></tr>
          <tr><td className="docs-ep-path">media_urls</td><td className="docs-ep-desc">Every link attached to the event (PDF, webcast, replay). <span className="docs-ic">url</span> is the first of these.</td></tr>
          <tr><td className="docs-ep-path">source_url</td><td className="docs-ep-desc">The IR page we found it on — useful for attribution and for spotting a bad parse.</td></tr>
          <tr><td className="docs-ep-path">discovered</td><td className="docs-ep-desc"><strong>When we first saw it. Always UTC with a trailing Z, regardless of <span className="docs-ic">tz</span>.</strong> This is your cursor — see below.</td></tr>
        </tbody>
      </table>

      <div className="docs-h3">How an event qualifies</div>
      <p className="docs-p">
        An event appears when <strong>both</strong> are true: we discovered it inside the window, <strong>and</strong> the
        period named by <span className="docs-ic">event_date</span> is the current one. Coarse dates count when the
        period they name contains today — in July, <span className="docs-ic">2026-Q2</span> counts because that is the
        quarter being reported, and <span className="docs-ic">2026</span> counts all year.
      </p>
      <p className="docs-p">
        That is why <span className="docs-ic">count</span> is much smaller than <span className="docs-ic">fetched</span>:
        we crawl a great deal and surface only what is current. If a bucket looks quiet, compare the two —{" "}
        <span className="docs-ic">fetched</span> at zero means the crawler was idle; a healthy{" "}
        <span className="docs-ic">fetched</span> with <span className="docs-ic">count</span> at zero just means nothing
        current turned up. <strong>An empty <span className="docs-ic">minute</span> bucket is normal</strong> — over a
        sampled day only about 9% of minutes had a qualifying event.
      </p>

      <div className="docs-h3">Polling for new events</div>
      <p className="docs-p">
        Events are newest-first by <span className="docs-ic">discovered</span>. Keep the highest{" "}
        <span className="docs-ic">discovered</span> you have processed and drop anything at or below it on the next
        poll. Once a minute against <span className="docs-ic">bucket=hour</span> gives a wide safety margin against a
        restart or a brief network gap:
      </p>
      <Code>{`let seen = null;                                  // highest "discovered" processed so far

async function poll() {
  const r = await fetch("${API_BASE}/today/events?bucket=hour&limit=500");
  if (!r.ok) return;                              // 429 = backing off, 503 = upstream down; just retry later
  const { events, truncated } = await r.json();

  const fresh = events.filter(e => !seen || e.discovered > seen);
  if (fresh.length) seen = fresh[0].discovered;   // events[0] is the newest

  if (truncated) console.warn("more than 500 in the last hour — widen the poll");
  return fresh;                                   // oldest-last; reverse() if you want chronological
}

setInterval(poll, 60_000);`}</Code>
      <p className="docs-p docs-muted">
        <span className="docs-ic">discovered</span> is ISO-8601 UTC, so a plain string comparison orders correctly — no
        date parsing needed.
      </p>

      <div className="docs-h3">Errors</div>
      <table className="docs-ep-table">
        <thead><tr><th style={{ width: 62 }}>Code</th><th>When</th></tr></thead>
        <tbody>
          <tr><td className="docs-ep-path">400</td><td className="docs-ep-desc">Unknown <span className="docs-ic">bucket</span> or <span className="docs-ic">tz</span>, or a non-numeric <span className="docs-ic">limit</span>. The body lists what is allowed.</td></tr>
          <tr><td className="docs-ep-path">429</td><td className="docs-ep-desc">More than 60 requests in 60 seconds from your IP. The body carries the limit and the window; wait and retry.</td></tr>
          <tr><td className="docs-ep-path">503</td><td className="docs-ep-desc">The database is unreachable. Deliberately vague — upstream error text is never forwarded. Retry with backoff.</td></tr>
        </tbody>
      </table>

      <div className="docs-h3">GET /today/pulse</div>
      <p className="docs-p">
        The same windows, counted rather than listed. Use it as a cheap change-detector: poll the pulse, and only fetch
        the feed when a bucket moves. <span className="docs-ic">buckets</span> applies the current-period filter;{" "}
        <span className="docs-ic">fetched</span> does not.
      </p>
      <Code>{`curl "${API_BASE}/today/pulse"

{
  "tz": "UTC", "now": "2026-07-28T07:59:15",
  "anchors": { "today": "2026-07-28", "quarter": "2026-Q3", "last_quarter": "2026-Q2", ... },
  "buckets": { "minute": 0, "hour": 19, "day": 257 },     // current-period events
  "fetched": { "minute": 8, "hour": 1145, "day": 28289 }, // everything crawled
  "period_matched_today": 257
}`}</Code>

      <div className="section-label">Try it live<span className="rule" /></div>
      <TryIt />

      <div className="docs-footer">
        <p>
          Read-only: there is no write path on this surface. The service holds no database connection of its own — it
          reads through a separate pool, so hitting it hard slows nothing down for the crawler. Questions, or need a
          window or field we do not expose yet? Ask and we will add it.
        </p>
      </div>
    </div>
  );
}
