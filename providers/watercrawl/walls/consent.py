"""watercrawl.walls.consent — dismiss a cookie / GDPR consent banner so it stops covering content + the screenshot.

用一句话讲完: 很多 IR 站首屏弹一个 cookie/GDPR 同意条(OneTrust/Cookiebot/Quantcast/…),它遮住内容、也污染喂给 VL 的
截图 —— 这里注入一段 JS,优先点已知 CMP 框架的"接受"按钮(id 稳定),再退回"consent 容器里文案是 Accept/同意/OK 的按钮"
兜底,把它关掉。WHY 只在 consent 语境点: 盲点任意 "Accept/OK/Continue" 会误触表单提交 —— 所以只认已知 CMP id + 明确带
cookie/consent/gdpr 语义的容器内按钮,普通页无匹配就 no-op。{GAP audit 2026-07-23 item #3 "cookie-consent 弹层自动关"}
[CONFIDENCE: CONFIRMED — 已知 CMP id 是行业标准;容器-scoped 文本兜底避免误点].
"""
from __future__ import annotations

# One evaluate() that (1) clicks a known-CMP accept button by its STABLE id/selector, else (2) finds a button whose
# text is an accept-verb AND whose ancestor looks like a consent banner (class/id/aria carries cookie|consent|gdpr|
# privacy). Returns the strategy that fired (or '') for the debug trail. Kept as one injected script so it runs in the
# page's own context across the common CMPs.
_DISMISS_JS = r"""() => {
  // 1) known Consent-Management-Platform accept buttons — stable ids/selectors, safest + most common
  const CMP = [
    '#onetrust-accept-btn-handler',                 // OneTrust (by far the most common on IR sites)
    '#accept-recommended-btn-handler',              // OneTrust (alt)
    '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',  // Cookiebot
    '#CybotCookiebotDialogBodyButtonAccept',        // Cookiebot (alt)
    '.cky-btn-accept',                              // CookieYes
    '#truste-consent-button',                       // TrustArc
    '.qc-cmp2-summary-buttons button[mode="primary"]',  // Quantcast
    'button#hs-eu-confirmation-button',            // HubSpot
    '[data-testid="uc-accept-all-button"]',        // Usercentrics
    '.osano-cm-accept-all',                        // Osano
    '#didomi-notice-agree-button',                 // Didomi
  ];
  for (const sel of CMP) {
    const el = document.querySelector(sel);
    if (el && el.offsetParent !== null) { try { el.click(); return 'cmp:' + sel; } catch (e) {} }
  }
  // 2) generic fallback: an accept-verb button INSIDE a consent-looking container (avoids clicking a random OK/Continue)
  const ACCEPT = /^(accept( all| cookies| all cookies)?|i (agree|accept)|agree|allow all|got it|ok|understand|同意|接受|同意并继续|모두 동의|동의|同意する|承認)$/i;
  const CTXRE = /cookie|consent|gdpr|privacy|cmp|banner|notice/i;
  const btns = document.querySelectorAll('button, a[role="button"], [role="button"], input[type="button"], input[type="submit"]');
  for (const b of btns) {
    if (b.offsetParent === null) continue;               // visible only
    const t = (b.innerText || b.textContent || b.value || '').replace(/\s+/g, ' ').trim();
    if (!ACCEPT.test(t)) continue;                        // text must be an explicit accept verb
    // require a consent-looking ancestor within 5 hops so we never click an accept button that isn't a cookie banner
    let n = b, hops = 0, inCtx = false;
    while (n && hops < 5) {
      const sig = ((n.className && n.className.toString ? n.className.toString() : '') + ' ' + (n.id || '') + ' ' + (n.getAttribute && (n.getAttribute('aria-label') || '') || ''));
      if (CTXRE.test(sig)) { inCtx = true; break; }
      n = n.parentElement; hops++;
    }
    if (inCtx) { try { b.click(); return 'text:' + t.slice(0, 30); } catch (e) {} }
  }
  return '';
}"""


async def dismiss_consent(page, dbg: dict | None = None) -> bool:
    """Click the page's cookie/GDPR accept button if one is showing → True if a banner was dismissed. Best-effort +
    safe: only fires on known CMP ids or an accept-verb button inside a consent-looking container, so a normal page is
    a no-op. Runs on the given async Page. {GAP audit item #3}."""
    try:
        fired = await page.evaluate(_DISMISS_JS)
        if fired:
            if dbg is not None:
                dbg["consent"] = fired                    # record which strategy dismissed it, for the trace
            return True
    except Exception:                                     # noqa: BLE001 — a CMP in a cross-origin iframe / eval hiccup → skip
        pass
    return False
