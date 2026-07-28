"""veracast (*.veracast.com) webcast register handler (async Playwright).

用一句话讲完: veracast 默认弹的是 username+E-mail+login 的 credential 登录墙(要真凭证),但同一个 modal 里
还有个 'Not registered?' 按钮能切到 self-service guest 注册表单 —— 所以策略是: 先走 guest 自助注册路径,
guest 路径不存在/失败时再 DETECT credential 登录墙并 flag dbg['wall'],避免一个 cred-only event 被误读成
'no media'。captions 是 SRT-via-API (globalVars.event.webcastCaptions[].path)。

veracast (OpenExchange Inc.) presents a LOGIN form by default — a `username` field + `E-mail` + a
`login` submit button — which IS a credential wall that needs real per-attendee creds. BUT the same
modal also exposes a `Not registered?` button that swaps the panel to a SELF-SERVICE guest registration
form (First/Last/Company/Classification/E-mail/verify-E-mail + privacy checkbox + Register) with NO
visible approval/pending gating — so the recovery is: try the guest self-register path first, and only
if that path is absent / fails, DETECT the credential login wall and flag it (dbg['wall']) instead of
silently looking like 'no media'. captions are SRT-via-API (globalVars.event.webcastCaptions[].path).
{LIVE PROBE 2026-06-14 bofa.veracast.com/.../globaltech2026: login form = <input name='username'> +
 'E-mail' + 'login' btn + 'Not registered?' btn(class='reg-form-link not-reg-btn'); after Not-registered
 click → guest form fields labelled First Name/Last Name/Company/Classification(select: Please Select One,
 Institutional Investor, Buyside Analyst, ...)/E-mail/Please verify E-mail + checkbox 'I have read and
 accept OpenExchange Inc.'s Privacy' + 'Register' btn; NO 'approval'/'pending'/'review' text seen}
[CONFIDENCE: CONFIRMED 90% — guest form is self-service (no approval words in probe); residual risk is a
 post-submit email-verification step (the verify-E-mail field hints at it) which would still be a wall —
 handled: if no media after Register we still flag the credential wall so callers don't see false 'no media']
"""
from __future__ import annotations

NAME = "veracast"


def matches(url: str) -> bool:
    # Any *.veracast.com webcast URL routes here. [CONFIDENCE: CONFIRMED 100% — host substring is stable]
    # matches 是纯函数,不做任何 IO / await —— 保持 sync def。
    return "veracast" in (url or "")


async def register(page, frames, reg, dbg, fill_in) -> bool:
    """Try the 'Not registered?' guest self-register path; else detect+flag the credential login wall.

    WHY this shape: veracast's default panel is a username+E-mail+login credential wall (needs real
    creds), but the same panel offers a 'Not registered?' link to a self-service guest form. We prefer
    the guest path (fillable with the demo registrant), and fall back to wall-detection + dbg['wall'] so
    a credential-only event is clearly flagged rather than mis-read as 'no media'.
    Upstream: called by _capture.py's capture loop for any veracast URL. Downstream: True → _capture skips
    the generic register fallback; False → generic register runs (and, for a flagged wall, the caller can
    surface 'needs creds' from dbg['wall']).
    {LIVE PROBE 2026-06-14: see module docstring for the exact field/button inventory}
    [CONFIDENCE: CONFIRMED 90% — every selector below was observed live; email-verify post-step is the gap]
    """
    # ---- helper: did the player actually load media? (network-resource check + DOM fallback) ----
    # 含 await(scope.evaluate / locator.count 都 async)→ 必须 async def,调用处也要 await。
    async def _has_player(scope):
        # PRIMARY signal: query the Resource Timing API for any .m3u8 the page has fetched. veracast's player
        # pulls the HLS manifest via fetch/XHR (NOT a <video src>), so a DOM <video> may never appear even on
        # success — but performance.getEntriesByType('resource') records the .m3u8 request reliably.
        # {LIVE VERIFY 2026-06-14: after guest Register the network captured 2 signed .m3u8 yet NO <video>
        #  mounted in the DOM — so DOM-only detection false-negatived; the resource-timing check fixes it}
        # [CONFIDENCE: CONFIRMED 90% — the DOM-only check missed the confirmed m3u8; resource-timing sees it]
        try:
            got_m3u8 = await scope.evaluate(  # async: evaluate 现在要 await
                "() => { try { return performance.getEntriesByType('resource')"
                ".some(e => (e.name||'').includes('.m3u8')); } catch(_) { return false; } }"
            )
            if got_m3u8:
                return True
        except Exception:
            pass
        # FALLBACK: an actual <video> / hls source in the DOM also means the player bootstrapped.
        try:
            if await scope.locator("video").count() > 0:  # async: .count() 要 await
                return True
            return await scope.locator("source[src*='.m3u8'], [src*='.m3u8']").count() > 0  # async: .count() 要 await
        except Exception:
            return False

    # ---- helper: poll all frames for a player over a window (the Register POST + player bootstrap is async) ----
    # 含 await(_has_player / page.wait_for_timeout)→ 必须 async def。
    async def _wait_player(seconds):
        # WHY: m3u8 fires several seconds AFTER the Register click (async POST → player bootstrap), so a single
        # immediate check false-negatives. Poll up to `seconds` so a successful guest entry isn't mis-flagged as a wall.
        # {LIVE VERIFY 2026-06-14: register() returned before <video> mounted yet m3u8 fired ~15s later}
        # [CONFIDENCE: CONFIRMED 90% — the false-negative was observed exactly this way and fixed by polling]
        waited = 0
        while waited < seconds * 1000:
            # page.frames 是 property(不 await);_has_player 是 async coroutine → 每次调用 await。
            # 注意: any(...) 不能包 async 生成器,所以把 frame 检查展开成显式 loop。
            if await _has_player(page):  # 主 page 先查
                return True
            for f in page.frames:  # page.frames 是 property,不 await
                if await _has_player(f):  # 每个 frame 逐个 await
                    return True
            await page.wait_for_timeout(1000)  # async: wait_for_timeout 要 await;1s poll interval
            waited += 1000
        return False

    # ---- step 0: dismiss the cookie banner + the blocking #veraModal info dialog ----
    # WHY: live probe showed a <div id='veraModal' class='visible'> overlay intercepts ALL pointer events,
    # so neither 'Not registered?' nor 'login' is clickable until it's gone.
    # {PROBE2: "<div id='veraModal' class='visible'> subtree intercepts pointer events"}
    # [CONFIDENCE: CONFIRMED 95% — click timed out on the overlay until removed via JS in probe3]
    try:
        ok = page.get_by_role("button", name="Ok", exact=False)  # cookie 'Ok' button (class btn bg-info);get_by_role 是 property-style,不 await
        if await ok.count() > 0:  # async: .count() 要 await
            await ok.first.click(timeout=2500)  # async: .click() 要 await;accept cookies
            dbg["clicked"] = "cookie:Ok"
            await page.wait_for_timeout(600)  # async: wait_for_timeout 要 await
    except Exception:
        pass  # cookie banner is best-effort; modal removal below is the real unblock
    try:
        # Hard-remove any modal/backdrop/overlay so subsequent clicks land on the form.
        await page.evaluate(  # async: evaluate 要 await
            "() => { document.querySelectorAll("
            "'#veraModal,.modal,[class*=modal],[class*=backdrop],[class*=overlay]'"
            ").forEach(m => m.remove()); }"
        )
        await page.wait_for_timeout(300)  # async: wait_for_timeout 要 await
    except Exception:
        pass

    # ---- step 1: detect the credential login wall up-front (so we can flag it even if guest path fails) ----
    # WHY: the default panel is a username/E-mail/login credential form. We record its presence now; if the
    # guest path doesn't land a player, we surface this as dbg['wall'].
    # {PROBE: <input name='username'> + 'login' submit button + page text 'E-mail\nlogin Not registered?'}
    # [CONFIDENCE: CONFIRMED 100% — username input + login button observed in every probe run]
    has_username = False
    has_login_btn = False
    try:
        has_username = await page.locator("input[name='username']").count() > 0  # async: .count() 要 await;the credential username field
    except Exception:
        pass
    try:
        # 'login' submit button (text 'login', class 'login-btn-txt')
        has_login_btn = await page.get_by_role("button", name="login", exact=False).count() > 0  # async: .count() 要 await
    except Exception:
        pass

    # ---- step 1b: if a registrant carries veracast creds, try the real login first (strongest path) ----
    # WHY: a credentialed event can ONLY be passed with real creds; if reg supplies them, use them.
    # {CONTRACT: 'reg dict 若带 veracast 凭证则用之' — reg may carry veracast username/passcode}
    # [CONFIDENCE: INFERRED 60% — cred field names not observed live (no creds available); username+passcode
    #  are the documented veracast credential pair, so we target those plus generic E-mail/password fallbacks]
    vc_user = reg.get("veracast_username") or reg.get("username")  # caller-supplied veracast username
    vc_pass = reg.get("veracast_passcode") or reg.get("passcode") or reg.get("password")  # the passcode
    if vc_user and vc_pass and has_username:
        try:
            await page.locator("input[name='username']").first.fill(str(vc_user), timeout=2500)  # async: .fill() 要 await;username
            dbg["filled"].append("veracast_username")
            # passcode field: try name='passcode', then any password input, then the labelled E-mail field
            filled_pass = False
            for sel in ["input[name='passcode']", "input[type='password']", "input[name='password']"]:
                try:
                    loc = page.locator(sel)  # locator() 构造不 await
                    if await loc.count() > 0:  # async: .count() 要 await
                        await loc.first.fill(str(vc_pass), timeout=2000)  # async: .fill() 要 await
                        filled_pass = True
                        dbg["filled"].append("veracast_passcode")
                        break
                except Exception:
                    pass
            if not filled_pass:
                # fall back to the shared helper against any E-mail/passcode label
                # fill_in 现在是 async → await。
                await fill_in(page, ["Passcode", "Password", "E-mail", "Email"], str(vc_pass))
            # submit the login form
            try:
                await page.get_by_role("button", name="login", exact=False).first.click(timeout=2500)  # async: .click() 要 await
                dbg["clicked"] = "login"
            except Exception:
                pass
            if await _wait_player(10):  # async: _wait_player 是 coroutine → await;poll up to 10s for the player after the cred-login POST
                return True  # creds worked — player reached
        except Exception:
            pass  # cred login failed; fall through to guest path / wall flag

    # ---- step 2: open the guest self-register panel via 'Not registered?' (JS click bypasses stray overlays) ----
    # WHY: the guest path is the only credential-free way in; the button text is stable ('Not registered?').
    # {PROBE3: JS-clicking the 'not registered' button swapped the panel to the guest form (returned true)}
    # [CONFIDENCE: CONFIRMED 90% — js-click reliably swapped to the guest form in probe3/probe4]
    guest_opened = False
    try:
        guest_opened = bool(await page.evaluate(  # async: evaluate 要 await
            "() => { const b=[...document.querySelectorAll('button,a')]"
            ".find(e=>/not registered/i.test(e.innerText||'')); if(b){b.click();return true;} return false; }"
        ))
        if guest_opened:
            dbg["clicked"] = "Not registered?"
            await page.wait_for_timeout(3000)  # async: wait_for_timeout 要 await;the guest form renders client-side after the swap
    except Exception:
        pass

    # ---- step 3: fill + submit the guest registration form ----
    # WHY: guest form fields have NO name/id — only label text — so we fill via the shared label-based helper.
    # {PROBE4: labelled fields First Name/Last Name/Company/Classification(select)/E-mail/Please verify E-mail
    #  + checkbox 'I have read and accept OpenExchange Inc.'s Privacy' + 'Register' button}
    # [CONFIDENCE: CONFIRMED 90% — exact labels lifted from the probe4 dump]
    if guest_opened:
        try:
            # text fields by label (fill_in uses get_by_label/get_by_placeholder);fill_in 现在是 async → 每次 await
            if await fill_in(page, ["First Name", "First"], reg["first"]):
                dbg["filled"].append("first")
            if await fill_in(page, ["Last Name", "Last"], reg["last"]):
                dbg["filled"].append("last")
            if await fill_in(page, ["Company", "Organization", "Firm"], reg["company"]):
                dbg["filled"].append("company")
            # primary E-mail — labelled exactly 'E-mail'
            if await fill_in(page, ["E-mail", "Email"], reg["email"]):
                dbg["filled"].append("email")
            # confirm-email — separate field labelled 'Please verify E-mail'; fill_in stops at the FIRST
            # match so we target the verify label explicitly to avoid re-filling the primary field.
            if await fill_in(page, ["Please verify E-mail", "verify E-mail", "Confirm E-mail", "Re-enter"], reg["email"]):
                dbg["filled"].append("email_verify")

            # Classification <select>: pick a sensible non-default option (analyst-leaning demo registrant).
            # {PROBE4 opts: Please Select One, Institutional Investor, Buyside Analyst, Sellside Analyst, Employee, Media}
            # [CONFIDENCE: CONFIRMED 85% — option labels observed; 'Buyside Analyst' fits the demo registrant role]
            try:
                sel = page.locator("select").first  # the only visible select in the guest form;.first 是 property,不 await
                if await sel.count() > 0:  # async: .count() 要 await
                    for opt in ["Buyside Analyst", "Institutional Investor", "Sellside Analyst", "Media"]:
                        try:
                            await sel.select_option(label=opt, timeout=1500)  # async: .select_option() 要 await;select by visible label
                            dbg["filled"].append("classification:" + opt)
                            break
                        except Exception:
                            continue
            except Exception:
                pass

            # privacy-acceptance checkbox — required to enable Register.
            # {PROBE4: checkbox label "I have read and accept OpenExchange Inc.'s Privacy"}
            try:
                cb = page.locator("input[type='checkbox']").first  # .first 是 property,不 await
                if await cb.count() > 0:  # async: .count() 要 await
                    await cb.check(timeout=2000)  # async: .check() 要 await;tick the privacy consent box
                    dbg["filled"].append("privacy")
            except Exception:
                pass

            # submit the guest registration
            try:
                await page.get_by_role("button", name="Register", exact=False).first.click(timeout=2500)  # async: .click() 要 await
                dbg["clicked"] = "Register"
            except Exception:
                pass

            # did the player load? poll up to 18s — the guest Register POST fires the signed .m3u8 ~15s later.
            # {LIVE VERIFY 2026-06-14: guest path yielded 2 signed .m3u8 (3031_keynot_a8.m3u8) ~15s post-Register}
            # [CONFIDENCE: CONFIRMED 90% — observed live; 18s window covers the measured ~15s POST→media latency]
            if await _wait_player(18):  # async: _wait_player 是 coroutine → await
                dbg["guest_register"] = "ok"  # mark the credential-free entry for the audit
                return True  # guest self-register worked — player reached, skip generic fallback
        except Exception:
            pass  # guest submit failed; fall through to wall flag

    # ---- step 4: no player reached → flag the credential login wall so the caller doesn't see 'no media' ----
    # WHY: if we land here, either the guest path was absent/failed (likely an email-verification step the
    # demo registrant can't complete) or only the credential login exists — both are walls needing real input.
    # {PROBE: persistent <input name='username'> + 'login' button + no .m3u8 after every attempt}
    # [CONFIDENCE: CONFIRMED 95% — username+login wall present in every probe; m3u8 count stayed 0]
    if has_username or has_login_btn:
        dbg["wall"] = "veracast credential login — needs real creds"  # explicit, non-silent wall flag
        # surface what the wall consists of so the audit can see the username+passcode credential shape
        dbg["wall_fields"] = {
            "username": has_username,            # <input name='username'>
            "login_button": has_login_btn,       # 'login' submit
            "guest_path_seen": guest_opened,     # 'Not registered?' guest form existed but didn't yield media
            "passcode": True,                    # veracast login pairs username with a passcode (post-username step)
        }
    else:
        # No recognisable veracast wall AND no player — let the generic fallback try.
        dbg["wall"] = "veracast: no login form and no player detected"
    return False  # never reached the player → generic fallback runs; wall is flagged for the audit
