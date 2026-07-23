"""Q4 Events Platform (events.q4inc.com) webcast register handler.

用一句话讲完: 你打开 Q4 的 attendee 页 → 撞上一个多屏 account-choice wall → 这个 handler 走
GUEST path(点 'Continue without a Q4 account')→ 填 screen-2 的 guest 表单(First/Last/Email +
勾 individual-attendee 绕过 typeahead company + required role dropdown)→ submit → player 加载。
async 版:所有 Playwright 调用 await,`fill_in` 现在是 async。

A JS-rendered SPA with a MULTI-SCREEN account-choice wall: screen 1 offers 'Register with a Q4
Account' / 'Create a Q4 Account' / 'Continue without a Q4 account' (NO input fields). The generic
register greedily clicks 'Register' → the Q4-account login dead-end. This handler takes the GUEST path
('Continue without a Q4 account'), then fills the screen-2 guest form (First/Last/Email/Company +
typeahead company + a required role dropdown + checkbox) and submits, so the player loads.
{WORKFLOW could-not-fetch audit: events.q4inc.com/attendee, Q4 Events Platform v8.22.0; guest form on
screen 2 has placeholderless inputs + typeahead company + required dropdown} [CONFIDENCE: SINGLE-SRC —
diagnosed; implementation + live verification by the platform workflow]
"""
from __future__ import annotations

NAME = "q4inc"


def matches(url: str) -> bool:
    # 纯函数 URL 匹配,不涉及任何 async I/O → 保持 sync,逐字照搬源文件
    u = url or ""
    return "events.q4inc.com" in u or "q4inc.com/attendee" in u


async def register(pg, frames, reg, dbg, fill_in) -> bool:
    """Take the Q4 guest path + fill the screen-2 form to reach the player. True if it acted.

    WHY this exact flow (live-probed 2026-06-14 on events.q4inc.com/attendee/132279280, the
    'NVIDIA Q2 FY23 Earnings Call' event): the generic register clicks 'Register' greedily and
    lands on the Q4-account login dead-end. The guest path needs four deliberate steps:
      1. screen-1: click 'Continue without a Q4 account' (a role=button; the other two buttons
         'Register with a Q4 Account' / 'Create a Q4 Account' both lead to the login wall).
      2. screen-2 (URL becomes …/guest): fill First/Last/Email BY id — the inputs have NO
         placeholder and NO label-for, so the shared fill_in (placeholder/label based) can't
         hit them; the ids are stable: GuestRegistration{FirstName,LastName,Email}Input.
      3. tick 'I am an individual attendee' by clicking its LABEL (the <label> intercepts pointer
         events, so .check() on the input times out) — this flips BOTH the Company Name typeahead
         (which a bare .fill won't satisfy) AND the required Company Role dropdown to "(not
         required)" in one move, so we never have to fight the typeahead / readonly nui-select.
      4. click 'Register for this Event' → the player loads.
    {PROBE 2026-06-14 SCREEN-1 BTN "Continue without a Q4 account" cls=registration-box_withoutq4-button}
    {PROBE 2026-06-14 SCREEN-2 INPUT id="GuestRegistrationFirstNameInput" ph="" name="" — placeholderless+label-less → fill_in can't target it, id can}
    {PROBE-2 2026-06-14 ".check() on #GuestRegistrationInvestorCheckboxInput → Timeout: <label …> intercepts pointer events"}
    {PROBE-3 2026-06-14 after label-click checkbox: GuestRegistrationInstitutionLookupInput becomes disabled:true}
    {PROBE-4 2026-06-14 after checkbox: labels read "Company Name (not required)" / "Company Role (not required)"}
    [CONFIDENCE: CONFIRMED — 4-round live probe; each step's selector + side-effect observed in the real DOM]

    upstream: called by _capture.py's per-platform dispatch (handler_for matched events.q4inc.com).
    downstream: returning True makes _capture.py skip the generic fill+click and go straight to the
    play()/HLS-capture poll; returning False lets the generic flow run as fallback.
    """
    try:
        # STEP 1 — screen-1 guest path. Click the 'without a Q4 account' button; the other two
        # buttons go to the Q4 login wall. {PROBE-1: 3 buttons, only this one avoids login}
        # [CONFIDENCE: CONFIRMED — clicking it navigated URL …/attendee/<id> → …/attendee/<id>/guest]
        clicked_guest = False                                     # did we leave screen-1?
        for nm in ("Continue without a Q4 account", "Continue without a Q4", "without a Q4 account"):
            try:
                btn = pg.get_by_role("button", name=nm, exact=False)  # nui-button is a real <button> — locator 是 property,不 await
                if await btn.count() > 0:                         # await: .count() 是 async
                    await btn.first.click(timeout=4000)           # .first 是 property;.click() 要 await;navigates the SPA to /guest
                    clicked_guest = True
                    dbg["clicked"] = "guest:" + nm
                    break
            except Exception:
                pass                                             # try the next phrasing
        if not clicked_guest:
            dbg["wall"] = "q4: screen-1 'Continue without a Q4 account' button not found"
            return False                                         # let generic try (unlikely to help)

        # STEP 2 — wait for the screen-2 guest form, then fill by stable id (placeholderless inputs).
        # {PROBE-1: ids GuestRegistration{FirstName,LastName,Email}Input, all type=text}
        # [CONFIDENCE: CONFIRMED — fill by id succeeded in probe-2/3/4]
        try:
            await pg.wait_for_selector("#GuestRegistrationFirstNameInput", timeout=15000)  # await: SPA renders late
        except Exception:
            dbg["wall"] = "q4: guest form (#GuestRegistrationFirstNameInput) never rendered"
            return False
        await pg.wait_for_timeout(1200)                          # await: let the form settle before typing
        # Map demo registrant -> the three placeholderless id inputs.
        for sel, val in (("#GuestRegistrationFirstNameInput", reg.get("first", "Research")),
                         ("#GuestRegistrationLastNameInput", reg.get("last", "Analyst")),
                         ("#GuestRegistrationEmailInput", reg.get("email", "demo@example.com"))):
            try:
                await pg.fill(sel, val, timeout=4000)            # await: id selector — no placeholder/label needed
                dbg["filled"].append(sel.lstrip("#"))            # record what we filled for the report
            except Exception:
                pass                                             # a missing field shouldn't abort the rest

        # STEP 3 — tick 'I am an individual attendee' by clicking the LABEL (label intercepts the input's
        # pointer events). This flips Company Name + Company Role to "(not required)", so we skip the
        # typeahead + readonly role-dropdown entirely. {PROBE-3/4: label-click works, both go not-required}
        # [CONFIDENCE: CONFIRMED — is_checked()==True after label click; required→not-required observed]
        checked = False                                          # did the individual-attendee toggle take?
        # 每个 strategy 现在是 async lambda — playwright 动作要 await,所以 lambda 体用 await
        for how in (lambda: pg.click("#GuestRegistrationInvestorCheckboxLabel", timeout=3000),  # label first
                    lambda: pg.check("#GuestRegistrationInvestorCheckboxInput", force=True, timeout=3000),  # force fallback
                    lambda: pg.get_by_text("I am an individual attendee", exact=False).first.click(timeout=3000)):  # text fallback
            try:
                await how()                                      # await: 每个 strategy 都是一个 async playwright 动作
                if await pg.is_checked("#GuestRegistrationInvestorCheckboxInput"):  # await: is_checked() 是 async
                    checked = True
                    break
            except Exception:
                pass                                             # try the next strategy
        dbg["individual_attendee"] = checked                      # surface whether we bypassed company fields
        if not checked:
            # Couldn't bypass company fields → Company Name typeahead + required Role dropdown remain.
            # Best-effort: fill the institution lookup with the demo company so a submit might still pass.
            try:
                await pg.fill("#GuestRegistrationInstitutionLookupInput", reg.get("company", "FocusAlpha"), timeout=3000)  # await
            except Exception:
                pass
        await pg.wait_for_timeout(800)                           # await: let the "(not required)" relabel apply

        # STEP 4 — submit. {PROBE-1: button text 'Register for this Event', a real <button>}
        # [CONFIDENCE: CONFIRMED — button present + enabled in every probe]
        submitted = False                                        # did we click the final submit?
        try:
            sub = pg.get_by_role("button", name="Register for this Event", exact=False)  # locator 是 property,不 await
            if await sub.count() > 0:                            # await: .count() 是 async
                await sub.first.click(timeout=4000)              # .first property;.click() await;→ player should load / m3u8 should fire
                submitted = True
                dbg["clicked"] = "submit:Register for this Event"
        except Exception:
            pass
        if not submitted:
            dbg["wall"] = "q4: 'Register for this Event' submit not clickable after fill"
            return False

        # POST-SUBMIT OUTCOME. The submit fires POST /rest/v2/attendee → 200; the server accepts the
        # guest registration and the SPA swaps the form out for the player. Two outcomes are possible
        # and we annotate dbg honestly for each (never fake an m3u8, never mis-flag a wall):
        #   A) PLAYER LOADS with a <video> whose currentSrc is a Q4 static asset URL of the form
        #      static.events.q4inc.com/companyAssets/.../videos/.../videoRecordingLink<uuid>. For this
        #      on-demand "event summary" the media is a DIRECT PROGRESSIVE MP4, NOT an HLS .m3u8 — so
        #      _capture.py's .m3u8/.vtt request listener never fires even though playback works. We
        #      record the real media URL in dbg['media'] so the caller knows where the asset is.
        #   B) Still on …/guest with NO <video> → an email/approval access-link gate held the player
        #      back (a demo email can't receive the link in-session). Flag dbg['wall'].
        # {PROBE-6 2026-06-14 submit → POST https://attendees.events.q4inc.com/rest/v2/attendee → 200, submit button gone}
        # {PROBE-7 2026-06-14 post-submit: 1 <video>, currentSrc="https://static.events.q4inc.com/companyAssets/.../videos/.../videoRecordingLink<uuid>", readyState=4 (HAVE_ENOUGH_DATA), heading reverted to plain "NVIDIA Q2 FY23 Earnings Call" — gate passed, media is progressive MP4 not HLS}
        # [CONFIDENCE: CONFIRMED — video currentSrc + readyState=4 + heading change all observed live]
        await pg.wait_for_timeout(3500)                          # await: let the SPA swap form → player (or re-render the form)
        try:
            # Read the post-submit player state in one evaluate: is there a <video>, and its real src.
            vstate = await pg.evaluate("""() => {
                const v = document.querySelector('video');                       // the on-demand player
                return {
                    has_video: !!v,
                    src: v ? (v.currentSrc || v.src || '') : '',                  // resolved media URL
                    ready: v ? v.readyState : -1                                  // 4 = HAVE_ENOUGH_DATA
                };
            }""")                                                # await: evaluate() 是 async
            on_guest = "/guest" in (pg.url or "")                # pg.url 是 property,不 await;still on the form route?
            if vstate.get("has_video") and vstate.get("src"):
                # Outcome A — player loaded with a real media URL.
                dbg["media"] = vstate["src"][:300]              # the actual asset (progressive MP4 for on-demand)
                if "videoRecordingLink" in vstate["src"] or "/videos/" in vstate["src"]:
                    # The transcript-relevant media is a direct MP4, NOT an HLS manifest → _capture's
                    # .m3u8 listener won't (and shouldn't) match; surface that fact so it isn't read as failure.
                    dbg["note"] = ("q4 on-demand: player loaded, media is a progressive MP4 "
                                   "(videoRecordingLink) — no HLS .m3u8 for this asset")
            elif on_guest:
                # Outcome B — no player; the server gated us behind an emailed access link.
                dbg["wall"] = ("q4: guest form POSTed /rest/v2/attendee → 200 but no inline player "
                               "(email/approval access-link gate — a demo email cannot receive the "
                               "access link in-session)")
        except Exception:
            pass                                                 # detection is best-effort; never abort on it
        # Return True regardless: we drove the correct GUEST path and the server accepted the
        # registration (and, for outcome A, the player loaded). Returning False would let the generic
        # flow greedily click 'Register' and walk back into the Q4-account login dead-end — strictly
        # worse. dbg['media'] / dbg['note'] / dbg['wall'] above tell the caller exactly what happened.
        # {_capture.py: True → skip generic; generic would re-hit the Q4 login wall}
        return True                                              # guest path completed; player loaded or gated
    except Exception as e:
        # Never raise into _capture.py's dispatch (it only catches at the handler boundary loosely).
        dbg["q4_err"] = str(e)[:160]                             # record + fall back to generic
        return False
