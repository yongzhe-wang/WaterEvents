// POST /api/chat — a STREAMING passthrough to the fleet's own vLLM, for hand-testing prompts against the exact model
// and the exact settings the pipeline uses.
//
// 用一句话讲完: 浏览器把 {system, messages, ...} POST 到这里 → 这里原样转给 pod 上的 fair gateway(经 ir-media-8 上
// 已有的 gateway-tunnel,127.0.0.1:8010)→ vLLM 的 SSE chunk 一块不改地推回浏览器。它是一根管子,不解析、不重组、
// 不缓存 —— 因为任何一层加工都会让 playground 里看到的东西和管线真正拿到的东西产生差异,而消除那个差异正是它存在的理由。
//
// WHY no new infrastructure: ir-media-8 already holds three SSH tunnels to the pod, and this process runs on that box.
// {IR-MEDIA-8 systemctl 2026-08-07 "gateway-tunnel.service ... RunPod fair_gateway SSH tunnel (local 8010 -> pod
//  127.0.0.1:8010)"} [CONFIDENCE: CONFIRMED 100% — read off the running host].
//
// WHY the gateway (8010) and NOT the direct vLLM tunnel (8000): the direct port bypasses the weighted fair queue, so a
// human holding the enter key would become an unmetered channel with no share limit — the one thing the gateway exists
// to prevent. Going through it means playground traffic is visible in the same accounting as the two agents.
//
// Upstream: PlaygroundView's LLM half. Downstream: fair_gateway :8010 → vLLM (`qwen-vl`).

// EVERY parameter is read from the SAME env vars the python client reads, with the SAME defaults, because the point of
// this endpoint is to reproduce production rather than to approximate it. Copying the numbers as literals here would
// create a second source of truth that drifts silently the first time one side is tuned.
// {PROVIDERS/QWEN_LLM/CONFIG.PY "SERVED_NAME = OS.ENVIRON.GET(\"QWEN_SERVED_NAME\", \"QWEN-VL\")" ·
//  "BASE_URLS = ... OS.ENVIRON.GET(\"QWEN_BASE_URLS\", \"HTTP://127.0.0.1:8000/V1\")" ·
//  "TEMPERATURE = FLOAT(OS.ENVIRON.GET(\"QWEN_TEMPERATURE\", \"0.0\"))" ·
//  "MAX_TOKENS = INT(OS.ENVIRON.GET(\"QWEN_MAX_TOKENS\", \"16384\"))" ·
//  "MAX_INPUT_CHARS = INT(OS.ENVIRON.GET(\"QWEN_MAX_INPUT_CHARS\", \"48000\"))"}
// {USER 2026-08-07 "i want llm config to be same as produ so max toekmns.et.c"}
// [CONFIDENCE: CONFIRMED 100% — direct user directive; every default below was read out of config.py, not recalled.]
// THE ENDPOINT IS ITS OWN VARIABLE, NOT INHERITED. QWEN_BASE_URLS is deliberately NOT used for the base url even
// though every other setting on this page comes from the fleet's env, because on the web app's host that variable
// points at the DIRECT vLLM tunnel rather than the gateway:
// {IR-MEDIA-8 "/ETC/WATEREVENTS.ENV:QWEN_BASE_URLS=HTTP://127.0.0.1:8000/V1"} — port 8000 is vLLM itself
// {IR-MEDIA-8 systemctl "VLLM-TUNNEL.SERVICE ... (LOCAL 8000 -> ... 127.0.0.1:8000)" vs
//  "GATEWAY-TUNNEL.SERVICE ... (LOCAL 8010 -> POD 127.0.0.1:8010)"}
// Inheriting it would route the playground around the weighted fair queue AND fail authentication, since 8000 wants
// the real key while this endpoint sends a tenant NAME. Routing is a decision, not a model setting, so it gets a
// variable of its own and a default that states the intent.
// [CONFIDENCE: CONFIRMED 100% — both env value and both tunnel definitions read off the live host.]
const BASE = (process.env.PLAYGROUND_VLLM_BASE || "http://127.0.0.1:8010/v1").trim().replace(/\/+$/, "");
const MODEL = process.env.QWEN_SERVED_NAME || "qwen-vl";

// THE BEARER TOKEN IS THE TENANT NAME, NOT A CREDENTIAL. The fair gateway accounts each request against whatever
// string arrives in Authorization, and only swaps in the real vLLM key on the last hop:
//   {FAIR_GATEWAY.PY "KEY = (REQ.HEADERS.GET(\"AUTHORIZATION\", \"\") OR \"\").REPLACE(\"BEARER \", \"\").STRIP() OR \"ANON\""}
//   {FAIR_GATEWAY.PY "FWD_HEADERS[\"AUTHORIZATION\"] = F\"BEARER {UPSTREAM_KEY}\""}
//   {FAIR_GATEWAY.PY "THE INCOMING AUTHORIZATION IS THE TENANT IDENTITY — IT IS WHAT THE FAIR SHARE IS ACCOUNTED
//    AGAINST, SO EVENT_AGENT AND MEDIA_AGENT MUST SEND DIFFERENT ONES"}
// The fleet does exactly this, one env file per unit:
//   {IR-MEDIA-8 "/ETC/WATEREVENTS/TENANT-EVENT.ENV:QWEN_API_KEY=EVENT_AGENT" ·
//    "/ETC/WATEREVENTS/TENANT-MEDIA.ENV:QWEN_API_KEY=MEDIA_AGENT"}
//
// So QWEN_API_KEY must NOT be reused here: this process inherits fleet.env, where that variable holds the REAL
// upstream secret {IR-MEDIA-8 "/ETC/WATEREVENTS/FLEET.ENV:QWEN_API_KEY=SK-WATEREVENTS-…"}. Sending it would put the
// playground in a bucket named after a secret and give it a share the weights never mention — the opposite of the
// visibility this endpoint is supposed to have. A literal tenant name is both correct and safe to have in this file,
// precisely because it is not a credential.
// [CONFIDENCE: CONFIRMED 100% — the gateway's key extraction, its upstream swap, and the two per-tenant env files
//  were each read directly; note that X-WE-Tenant is the RENDER VM's mechanism {TENANT_GATE.PY "HEADER =
//  \"X-WE-TENANT\""} and this gateway does not read it.]
const TENANT = process.env.PLAYGROUND_VLLM_TENANT || "playground";
const TEMPERATURE = Number(process.env.QWEN_TEMPERATURE ?? "0.0");
const MAX_TOKENS = Number(process.env.QWEN_MAX_TOKENS ?? "16384");
const MAX_INPUT_CHARS = Number(process.env.QWEN_MAX_INPUT_CHARS ?? "48000");
// The python client's timeout covers a whole non-streaming call. Here it is the budget to the FIRST byte only: once
// tokens are flowing the connection is demonstrably alive, and capping total duration would kill exactly the long
// generations (a 16k-token output) that this tool exists to inspect.
// {CONFIG.PY "REQUEST_TIMEOUT_S = INT(OS.ENVIRON.GET(\"QWEN_TIMEOUT_S\", \"120\"))"}
const FIRST_BYTE_TIMEOUT_MS = Number(process.env.QWEN_TIMEOUT_S ?? "120") * 1000;

/** The settings this endpoint will use, so the UI can display production's real values instead of guessing them. */
function defaults() {
  return { model: MODEL, temperature: TEMPERATURE, max_tokens: MAX_TOKENS,
           max_input_chars: MAX_INPUT_CHARS, base: BASE, tenant: TENANT,
           timeout_s: FIRST_BYTE_TIMEOUT_MS / 1000 };
}

export default async function handler(req, res) {
  // GET → the defaults, so the panel can render production's numbers on first paint with no round-trip guessing.
  if (req.method === "GET") {
    res.setHeader("Cache-Control", "no-store");
    return res.status(200).json(defaults());
  }
  if (req.method !== "POST") return res.status(405).json({ error: "POST or GET" });

  const b = req.body || {};
  const msgs = Array.isArray(b.messages) ? b.messages : [];
  if (!msgs.length) return res.status(400).json({ error: "messages required" });

  // The system turn is prepended rather than trusted from the client's array, so "which turn is the system prompt" has
  // exactly one answer and a UI bug cannot produce two of them.
  const messages = b.system ? [{ role: "system", content: String(b.system) }, ...msgs] : msgs;

  // Truncate the same way the python client does, and say so in the stream rather than silently. A prompt that was cut
  // produces a different answer, and a playground that hides the cut would teach the wrong lesson about the model.
  let truncated = 0;
  for (const m of messages) {
    if (typeof m.content === "string" && m.content.length > MAX_INPUT_CHARS) {
      truncated += m.content.length - MAX_INPUT_CHARS;
      m.content = m.content.slice(0, MAX_INPUT_CHARS);
    }
  }

  const payload = {
    model: b.model || MODEL,
    messages,
    stream: true,
    // vLLM only reports usage on a streamed request when asked; without this the token counts are simply absent.
    stream_options: { include_usage: true },
    temperature: b.temperature ?? TEMPERATURE,
    max_tokens: b.max_tokens ?? MAX_TOKENS,
  };
  // Guided decoding, so the ROUTE schema can be exercised exactly as the pipeline exercises it — the pipeline's JSON
  // never comes from a free-form model, and testing it free-form would test something the pipeline never does.
  if (b.json_schema) payload.guided_json = b.json_schema;

  res.setHeader("Content-Type", "text/event-stream; charset=utf-8");
  res.setHeader("Cache-Control", "no-cache, no-transform");    // no-transform: stop any proxy from buffering the stream
  res.setHeader("Connection", "keep-alive");
  res.setHeader("X-Accel-Buffering", "no");                    // nginx, should one ever land in front of this
  res.status(200).flushHeaders();

  const send = (obj) => res.write(`data: ${JSON.stringify(obj)}\n\n`);
  if (truncated) send({ warning: `input truncated by ${truncated} chars (QWEN_MAX_INPUT_CHARS=${MAX_INPUT_CHARS})` });

  // ONE controller aborts the upstream from either direction: the first-byte timer, or the browser going away. Without
  // the second, closing the tab leaves the GPU generating for a reader that no longer exists — and on a card already
  // preempting requests, that is capacity taken from the pipeline for nothing.
  // {VLLM /metrics 2026-08-07 "vllm:num_preemptions_total{...} 1198.0" with 36 running / 9 waiting}
  // [CONFIDENCE: CONFIRMED 100% — read from the live server while planning this endpoint.]
  const ac = new AbortController();
  const firstByteTimer = setTimeout(() => ac.abort("first-byte-timeout"), FIRST_BYTE_TIMEOUT_MS);
  let clientGone = false;
  req.on?.("close", () => { clientGone = true; ac.abort("client-closed"); });

  try {
    const upstream = await fetch(`${BASE}/chat/completions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        // The tenant name, per the block above. An unnamed request would land in the gateway's `anon` bucket at the
        // default weight of 1 {FAIR_GATEWAY.PY "RETURN _KEY_WEIGHTS.GET(KEY, 1.0)"} — bounded, but indistinguishable
        // from anything else unlabelled, which is why the name is sent explicitly rather than left to the default.
        Authorization: `Bearer ${TENANT}`,
      },
      body: JSON.stringify(payload),
      signal: ac.signal,
    });

    if (!upstream.ok || !upstream.body) {
      const detail = await upstream.text().catch(() => "");
      // The upstream's own words, verbatim and untruncated-in-meaning. A generic "upstream error" here would hide the
      // one thing being tested — vLLM says WHY it refused (context length, bad schema, unknown model) and that message
      // is the answer to the question the user just asked it.
      send({ error: `upstream ${upstream.status}`, detail: detail.slice(0, 2000) });
      return res.end();
    }

    const reader = upstream.body.getReader();
    const decoder = new TextDecoder();
    let first = true;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (first) { clearTimeout(firstByteTimer); first = false; }   // alive → the total-duration cap is deliberately absent
      if (clientGone) break;
      // Bytes through untouched. Decoding to text and re-encoding would risk splitting a multi-byte character across a
      // chunk boundary, and the browser reassembles SSE frames itself anyway.
      res.write(value);
      void decoder;                                                  // kept for the debugging path; not on the hot path
    }
    res.end();
  } catch (e) {
    clearTimeout(firstByteTimer);
    // An abort we caused is not a failure to report as one — the browser already knows it navigated away.
    if (!clientGone) {
      const why = ac.signal.reason === "first-byte-timeout"
        ? `no first token within ${FIRST_BYTE_TIMEOUT_MS / 1000}s — the model is queued behind the fleet`
        : String((e && e.message) || e);
      send({ error: "stream failed", detail: why });
    }
    try { res.end(); } catch { /* socket already gone */ }
  } finally {
    clearTimeout(firstByteTimer);
  }
}
