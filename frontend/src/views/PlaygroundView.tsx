// Playground — two INDEPENDENT test surfaces on one page: the render VM on top, the LLM underneath.
//
// 用一句话讲完: 上半部分贴一个 url 跑真实的 render 入口,把 text / links / html 原样摊开;下半部分是一个流式聊天框,
// 用生产的同一批参数打同一个 vLLM。两块各自独立可用,中间只有一个「把 text 送到下面 ↓」的按钮当桥 —— 没有共享状态,
// 所以你可以只测渲染、只测 prompt,或者串起来复现管线完整的一次调用。
//
// WHY two halves instead of one combined tool: they answer different questions and fail for different reasons. "Did
// the page render?" is about browsers, walls and JS; "did the model judge it right?" is about the prompt. A combined
// view forces you to pay the render cost to ask a prompt question, and hides which half broke when the answer is bad.
// {USER 2026-08-07 "let's plan these two separtealy so that page have two parts one is for render which is give a url
//  and output the ouptut and the other is just llm, so i can test freely"}
// [CONFIDENCE: CONFIRMED 100% — direct user directive on the page's shape.]
//
// 上游触发: nav 里的 Playground 项。下游连接: /api/render(→ ir-render-16:8100)、/api/chat(→ fair gateway :8010 →
// vLLM)、/api/prompts(→ backend 的 prompts.py)。
import { useEffect, useRef, useState } from "react";

// ── Shapes ───────────────────────────────────────────────────────────────────────────────────────────────────────
interface RenderOut {
  entry: string; url: string; elapsed_ms: number;
  text?: string; links?: string[]; html?: string;
  method?: string;                       // ONLY render_shot returns a real one: render|impersonate|camoufox|walled|empty
  shot_b64?: string; shot_b64_bytes?: number; inline?: string;
  // fetch_doc returns a DocResult instead of a render tuple.
  markdown?: string; tables?: unknown[]; n_pages?: number; via?: string; warnings?: string[]; error?: string;
  detail?: string;
}
interface ChatDefaults {
  model: string; temperature: number; max_tokens: number; max_input_chars: number; base: string;
  tenant: string; timeout_s: number;
}
interface Preset { name: string; source: string; text: string }
type Msg = { role: "user" | "assistant"; content: string };

const ENTRIES = ["render_detail", "render_full", "render_shot", "expand_events_page", "fetch_doc"] as const;
type Entry = (typeof ENTRIES)[number];

// What each entry is FOR, in one line. The choice between render_full and render_detail is a real diagnostic decision
// — they are separate functions with different success tests — so the UI states the difference rather than implying
// they are interchangeable. {ORCHESTRATOR.PY "CODE-LEVEL ISOLATION FROM RENDER_FULL (A SEPARATE FUNCTION, NOT A BOOL
// FLAG): RENDER_FULL SUCCEEDS ON LINK COUNT (DISCOVERY); RENDER_DETAIL ON CONTENT PRESENCE"}
const ENTRY_HELP: Record<Entry, string> = {
  render_detail: "stage-2 用的:内容优先,fallback 判据是「有没有真正的正文」",
  render_full: "stage-1 发现用:链接优先,fallback 判据是「链接多不多」",
  render_shot: "带整页截图,并返回真实的 method(哪一级 fallback 赢了)",
  expand_events_page: "驱动年份筛选 + load-more,60-80s,专治只显示 3-5 条的列表页",
  fetch_doc: "文档路径:在 render VM 上下载 → Docling 解析 → md + tables",
};

function num(n: number | null | undefined): string {
  return n == null ? "—" : n.toLocaleString();
}

// ── RENDER half ──────────────────────────────────────────────────────────────────────────────────────────────────
function RenderPanel({ onSendText }: { onSendText: (text: string, url: string) => void }) {
  const [url, setUrl] = useState("");
  const [entry, setEntry] = useState<Entry>("render_detail");
  const [wantShot, setWantShot] = useState(false);
  const [busy, setBusy] = useState(false);
  const [waited, setWaited] = useState(0);
  const [out, setOut] = useState<RenderOut | null>(null);
  const [tab, setTab] = useState("text");
  const [linkFilter, setLinkFilter] = useState("");

  // A ticking "已等 Ns" while in flight. expand_events_page is documented at 60-80s and fetch_doc can run for minutes,
  // so a spinner with no elapsed time is indistinguishable from a hang for exactly the entries that legitimately take
  // longest — the misreading this page exists to prevent.
  useEffect(() => {
    if (!busy) return;
    const t0 = Date.now();
    const id = setInterval(() => setWaited(Math.round((Date.now() - t0) / 1000)), 250);
    return () => clearInterval(id);
  }, [busy]);

  const run = async () => {
    if (!url.trim() || busy) return;
    setBusy(true); setOut(null); setWaited(0);
    try {
      const r = await fetch("/api/render", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url.trim(), entry, want_shot: wantShot }),
      });
      const j: RenderOut = await r.json();
      setOut(j);
      setTab(j.markdown != null ? "markdown" : j.inline != null ? "inline" : "text");
    } catch (e) {
      setOut({ entry, url, elapsed_ms: 0, error: "request failed", detail: String(e) });
    } finally { setBusy(false); }
  };

  // Tabs are built from what CAME BACK, not from a fixed list, because the five entries return genuinely different
  // shapes — a render tuple, a DocResult, or a bare `inline` string. Showing an empty "tables" tab for render_detail
  // would suggest the field exists and is empty; it does not exist.
  const tabs: { key: string; label: string; body: string }[] = [];
  if (out && !out.error) {
    if (out.text != null) tabs.push({ key: "text", label: `text ${num(out.text.length)}`, body: out.text });
    if (out.markdown != null) tabs.push({ key: "markdown", label: `md ${num(out.markdown.length)}`, body: out.markdown });
    if (out.inline != null) tabs.push({ key: "inline", label: `inline ${num(out.inline.length)}`, body: out.inline });
    if (out.links != null) tabs.push({ key: "links", label: `links ${num(out.links.length)}`, body: "" });
    if (out.tables != null) tabs.push({ key: "tables", label: `tables ${num(out.tables.length)}`, body: JSON.stringify(out.tables, null, 1) });
    if (out.html) tabs.push({ key: "html", label: `html ${num(out.html.length)}`, body: out.html });
    if (out.shot_b64) tabs.push({ key: "shot", label: "shot", body: "" });
    tabs.push({ key: "raw", label: "raw json", body: JSON.stringify({ ...out, shot_b64: out.shot_b64 ? "<omitted>" : undefined, html: out.html ? `<${out.html.length} chars>` : undefined }, null, 1) });
  }
  const active = tabs.find((t) => t.key === tab) || tabs[0];
  const links = (out?.links || []).filter((l) => !linkFilter || l.toLowerCase().includes(linkFilter.toLowerCase()));

  return (
    <div className="q-card pg-panel">
      <div className="q-card-head">
        <span className="q-card-title">Render</span>
        <span className="q-card-sub">ir-render-16 · tenant <code>playground</code></span>
      </div>

      <div className="pg-row">
        <input className="pg-input pg-grow" placeholder="https://investor.example.com/news/2025-q4-results"
               value={url} onChange={(e) => setUrl(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter") run(); }} />
        <button className="pg-btn pg-btn-go" onClick={run} disabled={busy || !url.trim()}>
          {busy ? `跑着… ${waited}s` : "跑"}
        </button>
      </div>

      <div className="pg-row pg-wrap">
        {ENTRIES.map((e) => (
          <label key={e} className={`pg-radio${entry === e ? " on" : ""}`} title={ENTRY_HELP[e]}>
            <input type="radio" name="entry" checked={entry === e} onChange={() => setEntry(e)} />
            {e}
          </label>
        ))}
        <label className="pg-radio" title="整页 JPEG,可能几 MB,默认不传回浏览器">
          <input type="checkbox" checked={wantShot} onChange={(e) => setWantShot(e.target.checked)} />
          要截图
        </label>
      </div>
      <div className="pg-help">{ENTRY_HELP[entry]}</div>

      {out?.error && (
        <div className="pg-err">
          <strong>{out.error}</strong>
          {out.detail ? <div className="pg-err-detail">{out.detail}</div> : null}
        </div>
      )}

      {out && !out.error && (
        <>
          <div className="pg-row pg-wrap pg-meta">
            <span className="chip">{out.elapsed_ms} ms</span>
            {/* Only render_shot's `method` is a method. The other entries' third field was renamed to `html`
                precisely because it never was one. */}
            {out.method ? <span className="chip">method {out.method}</span> : null}
            {out.via ? <span className="chip">via {out.via}</span> : null}
            {out.n_pages != null ? <span className="chip">{out.n_pages} pages</span> : null}
            {out.shot_b64_bytes ? <span className="chip">shot {Math.round(out.shot_b64_bytes / 1024)} KB(未传回)</span> : null}
            {(out.warnings || []).map((w, i) => <span key={i} className="chip pg-chip-warn">{w}</span>)}
            {out.text ? (
              <button className="pg-btn pg-btn-bridge" onClick={() => onSendText(out.text || "", out.url)}>
                把 text 送到下面的 LLM ↓
              </button>
            ) : null}
          </div>

          <div className="pg-tabs">
            {tabs.map((t) => (
              <button key={t.key} className={`pg-tab${active?.key === t.key ? " on" : ""}`} onClick={() => setTab(t.key)}>
                {t.label}
              </button>
            ))}
          </div>

          {active?.key === "links" ? (
            <>
              {/* Searchable on purpose: the question this list answers is almost always "is THIS url in here?" —
                  the document-adoption gate refuses any proposal absent from it, so scanning by eye is the slow way
                  to answer the exact question that matters. */}
              <input className="pg-input pg-filter" placeholder={`过滤 ${out.links?.length || 0} 个链接…`}
                     value={linkFilter} onChange={(e) => setLinkFilter(e.target.value)} />
              <div className="pg-out pg-links">
                {links.map((l, i) => <div key={i} className="pg-link">{l}</div>)}
                {!links.length ? <div className="pg-help">没有匹配的链接</div> : null}
              </div>
            </>
          ) : active?.key === "shot" ? (
            <img className="pg-shot" alt="rendered page" src={`data:image/jpeg;base64,${out.shot_b64}`} />
          ) : (
            <pre className="pg-out">{active?.body || "(空)"}</pre>
          )}
        </>
      )}
    </div>
  );
}

// ── LLM half ─────────────────────────────────────────────────────────────────────────────────────────────────────
function LlmPanel({ seed }: { seed: { text: string; url: string; n: number } }) {
  const [defs, setDefs] = useState<ChatDefaults | null>(null);
  const [defsErr, setDefsErr] = useState("");
  const [presets, setPresets] = useState<Preset[]>([]);
  const [system, setSystem] = useState("");
  const [systemOpen, setSystemOpen] = useState(false);
  const [input, setInput] = useState("");
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [temp, setTemp] = useState<number | null>(null);
  const [maxTok, setMaxTok] = useState<number | null>(null);
  const [ttft, setTtft] = useState<number | null>(null);
  const [tps, setTps] = useState<number | null>(null);
  const [usage, setUsage] = useState<{ prompt: number; completion: number } | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  // Production's own numbers, fetched rather than assumed. The whole reason this panel exists is to reproduce what the
  // pipeline sends, so the initial values must come from the same env the pipeline reads.
  // {USER 2026-08-07 "i want llm config to be same as produ so max toekmns.et.c"}
  useEffect(() => {
    fetch("/api/chat").then((r) => r.json()).then((d: ChatDefaults) => {
      // SHAPE-CHECK, do not trust "it parsed as json". A 401 from the app's basic-auth gate, a proxy error page, or
      // any handler that returns {error} all decode to a perfectly valid object whose fields are simply absent — and
      // rendering that object produces "undefined · undefined · tenant undefined" in the header, which reads as a
      // broken model rather than a failed fetch. Observed exactly that on the local shim before this guard existed.
      // [CONFIDENCE: CONFIRMED 100% — reproduced in the browser against a 401 from dev-api-server's auth gate.]
      if (!d || typeof d.model !== "string") throw new Error("bad /api/chat response");
      setDefs(d); setTemp(d.temperature); setMaxTok(d.max_tokens);
    }).catch((e) => setDefsErr(String((e as Error).message || e)));
    fetch("/api/prompts").then((r) => r.json())
      .then((d) => setPresets(Array.isArray(d?.prompts) ? d.prompts : []))
      .catch(() => setPresets([]));
  }, []);

  // A render result arriving from the panel above. `n` is a counter rather than a content check so sending the SAME
  // page twice still fires — re-running one url after a prompt change is a normal thing to do.
  useEffect(() => {
    if (!seed.n) return;
    const route = presets.find((p) => p.name === "SYSTEM_ROUTE");
    if (route) { setSystem(route.text); setSystemOpen(true); }
    // The exact envelope build_user() produces, so what the model reads here is what it reads in the pipeline.
    // {PROMPTS.PY "RETURN F\"PAGE URL: {PAGE_URL}\\N\\N{REF}\\N\\N{BODY}\""} with ref = the KNOWN EVENT block.
    setInput(
      `PAGE URL: ${seed.url}\n\n` +
      `KNOWN EVENT (reference — confirm & extend, do not blindly trust):\n` +
      `  title: \n  date: \n  type: \n  media urls already found: ['${seed.url}']\n\n` +
      `PAGE CONTENT (reading order):\n${seed.text}`
    );
  }, [seed.n]);                                     // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { bottomRef.current?.scrollIntoView({ block: "end" }); }, [msgs, streaming]);

  const stop = () => { abortRef.current?.abort(); setStreaming(false); };

  const send = async () => {
    if (!input.trim() || streaming) return;
    const outgoing: Msg[] = [...msgs, { role: "user", content: input }];
    setMsgs([...outgoing, { role: "assistant", content: "" }]);
    setInput(""); setStreaming(true); setTtft(null); setTps(null); setUsage(null);

    const ac = new AbortController();
    abortRef.current = ac;
    const t0 = performance.now();
    let firstAt: number | null = null;
    let acc = "";

    try {
      const r = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" }, signal: ac.signal,
        body: JSON.stringify({
          system: system || undefined, messages: outgoing,
          temperature: temp ?? undefined, max_tokens: maxTok ?? undefined,
        }),
      });
      if (!r.body) throw new Error("no response body");

      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        // SSE frames are separated by a BLANK LINE, and a chunk boundary can land anywhere — including mid-frame. Keep
        // the trailing partial in the buffer instead of parsing it, or a long token stream will throw on a split frame.
        const frames = buf.split("\n\n");
        buf = frames.pop() ?? "";
        for (const frame of frames) {
          const line = frame.split("\n").find((l) => l.startsWith("data:"));
          if (!line) continue;
          const data = line.slice(5).trim();
          if (data === "[DONE]") continue;
          let j: Record<string, unknown>;
          try { j = JSON.parse(data); } catch { continue; }

          if (j.error) { acc += `\n\n⛔ ${j.error}\n${j.detail ?? ""}`; }
          else if (j.warning) { acc += `⚠ ${j.warning}\n\n`; }

          const piece = (j as { choices?: { delta?: { content?: string } }[] }).choices?.[0]?.delta?.content;
          if (piece) {
            if (firstAt == null) { firstAt = performance.now(); setTtft(Math.round(firstAt - t0)); }
            acc += piece;
          }
          const u = (j as { usage?: { prompt_tokens: number; completion_tokens: number } }).usage;
          if (u) {
            setUsage({ prompt: u.prompt_tokens, completion: u.completion_tokens });
            // Measured from the FIRST TOKEN, not from the request, so queueing time is not silently averaged into a
            // generation rate. Those are two different numbers and ttft already reports the first one.
            const gen = (performance.now() - (firstAt ?? t0)) / 1000;
            if (gen > 0) setTps(Math.round((u.completion_tokens / gen) * 10) / 10);
          }
          setMsgs([...outgoing, { role: "assistant", content: acc }]);
        }
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        setMsgs([...outgoing, { role: "assistant", content: `${acc}\n\n⛔ ${String(e)}` }]);
      }
    } finally { setStreaming(false); abortRef.current = null; }
  };

  return (
    <div className="q-card pg-panel">
      <div className="q-card-head">
        <span className="q-card-title">LLM</span>
        <span className="q-card-sub">
          {defs ? `${defs.model} · ${defs.base} · tenant ${defs.tenant}`
                : defsErr ? `⛔ 拿不到配置: ${defsErr}` : "读取配置…"}
        </span>
      </div>

      <div className="pg-row pg-wrap pg-meta">
        <label className="pg-knob">temp
          <input className="pg-input pg-knob-in" type="number" step="0.1" min="0" max="2"
                 value={temp ?? ""} onChange={(e) => setTemp(e.target.value === "" ? null : Number(e.target.value))} />
        </label>
        <label className="pg-knob">max_tokens
          <input className="pg-input pg-knob-in" type="number" step="256" min="1"
                 value={maxTok ?? ""} onChange={(e) => setMaxTok(e.target.value === "" ? null : Number(e.target.value))} />
        </label>
        {/* Production's values, printed so a changed knob is visibly a deviation rather than an unknown. */}
        {defs ? <span className="q-card-sub">prod: temp {defs.temperature} · max_tokens {num(defs.max_tokens)} · 输入上限 {num(defs.max_input_chars)} 字符</span> : null}
        <span className="pg-spacer" />
        {ttft != null ? <span className="chip">首字 {ttft} ms</span> : null}
        {tps != null ? <span className="chip">{tps} tok/s</span> : null}
        {usage ? <span className="chip">in {num(usage.prompt)} · out {num(usage.completion)}</span> : null}
        {msgs.length ? <button className="pg-btn" onClick={() => { setMsgs([]); setUsage(null); setTtft(null); setTps(null); }}>清空</button> : null}
      </div>

      <div className="pg-sys">
        <button className="pg-tab on" onClick={() => setSystemOpen(!systemOpen)}>
          system {systemOpen ? "▾" : "▸"} {system ? `(${num(system.length)} 字符)` : "(空)"}
        </button>
        {presets.map((p) => (
          <button key={p.name} className="pg-tab" title={p.source} onClick={() => { setSystem(p.text); setSystemOpen(true); }}>
            {p.name}
          </button>
        ))}
        {system ? <button className="pg-tab" onClick={() => setSystem("")}>清空 system</button> : null}
      </div>
      {systemOpen && (
        <textarea className="pg-input pg-sys-box" value={system} onChange={(e) => setSystem(e.target.value)}
                  placeholder="system prompt — 留空就是没有 system turn" />
      )}

      <div className="pg-chat">
        {msgs.map((m, i) => (
          <div key={i} className={`pg-msg pg-msg-${m.role}`}>
            <div className="pg-msg-role">{m.role}</div>
            <pre className="pg-msg-body">{m.content}{streaming && i === msgs.length - 1 ? <span className="pg-caret">▌</span> : null}</pre>
          </div>
        ))}
        {!msgs.length ? <div className="pg-help">还没有对话。上面 Render 跑完之后可以一键把 text 灌进来,或者直接在下面打字。</div> : null}
        <div ref={bottomRef} />
      </div>

      <div className="pg-row">
        <textarea className="pg-input pg-grow pg-ask" value={input} rows={3}
                  placeholder="user turn — ⌘/Ctrl+Enter 发送"
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send(); } }} />
        <div className="pg-send-col">
          <button className="pg-btn pg-btn-go" onClick={send} disabled={streaming || !input.trim()}>发送</button>
          <button className="pg-btn" onClick={stop} disabled={!streaming}>停止</button>
        </div>
      </div>
    </div>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────────────────────────────────────────────
export default function PlaygroundView() {
  // The ONLY state the two halves share. A counter rather than a boolean so sending the same page twice still fires.
  const [seed, setSeed] = useState({ text: "", url: "", n: 0 });
  return (
    <div className="body-full">
      <RenderPanel onSendText={(text, url) => setSeed((s) => ({ text, url, n: s.n + 1 }))} />
      <LlmPanel seed={seed} />
    </div>
  );
}
