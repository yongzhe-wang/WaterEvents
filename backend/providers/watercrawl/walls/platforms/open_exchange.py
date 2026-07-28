"""open-exchange.net webcast register handler.

A React SPA whose register form's submit ('Register', type=submit) is DISABLED until a REQUIRED custom
dropdown ('Select an option' → confirm corporate email) is set — the generic register fills the text
fields but the disabled button never fires (clicked stays null). This handler operates the non-text
controls (open the custom dropdown, pick the affirmative option, set any required select/checkbox),
waits for the submit to enable, then clicks it. {WORKFLOW could-not-fetch audit: open-exchange register
Register button disabled:true behind a 'Select an option' dropdown} [CONFIDENCE: SINGLE-SRC — diagnosed;
implementation + live verification by the platform workflow. Note: may still be email/approval-gated.]
"""
from __future__ import annotations  # async annotations + 延后求值,不必 import 运行期类型

NAME = "open_exchange"


def matches(url: str) -> bool:
    return "open-exchange.net" in (url or "")


async def register(page, frames, reg, dbg, fill_in) -> bool:
    """Fill the 4 text fields + set the required 'Yes' custom-select to enable + click Register.

    用一句话讲完:open-exchange 是个 React SPA,submit 按钮 disabled,直到一个 required custom combobox
    选了 'Yes' —— 所以流程是 填 4 个文本字段 → 点开 combobox → 点 'Yes' option → 轮询等 submit
    从 disabled 翻成 enabled → 点 submit。generic handler 干不了这个(它只填文本 + 点 role=button,
    而 disabled 的 submit 永远不 fire),所以这个 platform handler 专门操作那些 non-text 控件。

    WHY: live probe (2026-06-14) showed the register form is plain on the MAIN frame (no iframe) with
    4 label-only text inputs (#firstName/#lastName/#email/#organizationName, NO placeholder, required=False)
    and a custom React combobox that gates a 'Register' submit button which starts disabled:True. The wall
    is NOT a <select> — it's a <button role=combobox class=custom-select__selected-item> reading
    'Select an option'; clicking it reveals a <ul role=listbox class=custom-select__options> whose ONLY
    real option is a <li role=option class=option>Yes</li>. Picking 'Yes' flips the submit's disabled
    True→False. The generic handler can't do this (it only fills text + clicks role=button by visible name,
    and a disabled submit never fires). Workflow: fill text → open combobox → click 'Yes' → wait submit
    enabled → click submit.
    {PROBE /tmp/probe_open_exchange.py 2026-06-14: "BUTTON type=submit text='Register' disabled:True" +
     "BUTTON cls='custom-select__selected-item' role='combobox' text='Select an option'" + inputs
     id=firstName/lastName/email/organizationName}
    {PROBE2 /tmp/probe2_open_exchange.py 2026-06-14: "OPTIONS after expand: role='option' text='Yes'" then
     "AFTER pick -> Register disabled: False  selectedText: Yes"}
    [CONFIDENCE: CONFIRMED 90% — both probes ran live, combobox→Yes→submit-enable chain verified; the
     residual 10% is a possible post-submit email/approval gate the probe could not exercise (event is
     future-dated 2026), in which case dbg['wall'] is set and we still return True for having reached past
     the form gate.]
    """
    try:
        # The register form lives on the MAIN frame (probe: FRAMES=1, no iframe) — operate page directly. [CONFIRMED 95%]
        # Fill the 4 text fields by id first (label-associated; id is the most stable selector since there are no placeholders).
        for fid, val, tag in [("#firstName", reg["first"], "first"),     # {PROBE: input id=firstName}
                              ("#lastName", reg["last"], "last"),         # {PROBE: input id=lastName}
                              ("#email", reg["email"], "email"),          # {PROBE: input id=email}
                              ("#organizationName", reg["company"], "company")]:  # {PROBE: input id=organizationName}
            try:
                loc = page.locator(fid)                                  # id selector — exact match, no label ambiguity
                if await loc.count() > 0:                                # async: .count() 现在是 coroutine,await 取真实计数
                    await loc.first.fill(val, timeout=3000)              # async: fill the demo registrant value
                    dbg["filled"].append(tag)                            # record which fields we filled for the audit
            except Exception:
                pass                                                     # one missing field shouldn't abort the whole flow

        # Open the custom combobox: <button role=combobox class=custom-select__selected-item>'Select an option'.
        # {PROBE2: clicking button[role=combobox] expands the listbox} [CONFIDENCE: CONFIRMED 90%]
        opened = False                                                    # track whether the dropdown actually expanded
        for sel in ["button[role=combobox]",                            # primary: the probed combobox role
                    ".custom-select__selected-item",                     # fallback: the class probe saw on that button
                    ".custom-select"]:                                    # last resort: the wrapper div
            try:
                cb = page.locator(sel)                                    # locate the combobox trigger
                if await cb.count() > 0:                                  # async: await .count() 取候选数
                    await cb.first.click(timeout=3000)                   # async: click to reveal the options listbox
                    opened = True; break                                 # stop at the first one that clicks
            except Exception:
                pass                                                     # try the next selector candidate
        await page.wait_for_timeout(900)                                 # async: React renders the listbox a beat after the click

        # Pick the affirmative option. Probe showed the ONLY real option is 'Yes' (li role=option class=option).
        # {PROBE2: "role='option' text='Yes'"} [CONFIDENCE: CONFIRMED 90%]
        picked = None                                                     # track which affirmative option we chose
        for txt in ["Yes", "I confirm", "Confirm", "Agree"]:            # 'Yes' is the live value; others are defensive
            try:
                # Prefer the role=option scope so we hit the listbox item, not the label text that contains the same word.
                opt = page.get_by_role("option", name=txt, exact=False)  # role=option is the semantic option element
                if await opt.count() == 0:                               # async: await .count() — fallback if role isn't exposed by the SPA
                    opt = page.locator(".option", has_text=txt)          # probe class on the option <li>
                if await opt.count() > 0:                                # async: await .count() 取匹配数
                    await opt.first.click(timeout=2500)                  # async: select the affirmative option
                    picked = txt; break                                  # stop at the first match
            except Exception:
                pass                                                     # try the next candidate text
        dbg["picked_option"] = picked                                    # record the chosen option for the audit

        # Wait for the submit (Register, type=submit) to flip disabled True→False once the option is set.
        # {PROBE2: "AFTER pick -> Register disabled: False"} [CONFIDENCE: CONFIRMED 90%]
        enabled = False                                                   # whether Register became clickable
        for _ in range(20):                                              # poll up to ~5s (React state propagation)
            try:
                state = await page.evaluate("""() => {
                    const r = Array.from(document.querySelectorAll('button[type=submit]'))[0];
                    return r ? !r.disabled : false;   // true when Register is enabled
                }""")                                                    # async: await evaluate 读 live disabled flag from the DOM
                if state:
                    enabled = True; break                                 # submit is enabled — exit the poll
            except Exception:
                pass
            await page.wait_for_timeout(250)                             # async: short backoff between polls
        dbg["submit_enabled"] = enabled                                  # record enable result for the audit

        # Click Register (type=submit) now that it's enabled.
        clicked = False                                                   # whether we actually clicked submit
        if enabled:
            for sel in ["button[type=submit]",                          # primary: the probed submit button
                        "button:has-text('Register')"]:                  # fallback by visible text
                try:
                    btn = page.locator(sel)                              # locate the submit
                    if await btn.count() > 0 and await btn.first.is_enabled():  # async: await .count() + await .is_enabled() double-check before clicking
                        await btn.first.click(timeout=3000)             # async: submit the registration
                        dbg["clicked"] = "Register"; clicked = True; break
                except Exception:
                    pass                                                 # try the next selector
        await page.wait_for_timeout(4000)                               # async: give the post-submit nav/player time to load

        # Detect a post-submit email/approval second gate: if the URL still shows /registration or a
        # 'check your email'/'thank you'/'confirm' message appears, flag the wall so _capture knows the
        # player wasn't reached but the form gate WAS passed. {DIAGNOSIS: "may still be email/approval gated"}
        # [CONFIDENCE: INFERRED 70% — event is future-dated 2026 so a live stream is unlikely to exist yet;
        #  marking the wall is the honest outcome vs claiming a false player-load.]
        try:
            gate = await page.evaluate("""() => {
                const t = (document.body.innerText||'').toLowerCase();
                const u = location.href.toLowerCase();
                const hit = /check your email|thank you for register|confirmation|we'?ll be in touch|approv|pending|verify your email/.test(t);
                const stillForm = u.includes('/registration');
                return {hit, stillForm, url: location.href.slice(0,120)};
            }""")                                                        # async: await evaluate scan post-submit page for second-gate signals
            if gate["hit"] or (gate["stillForm"] and clicked):           # second gate OR bounced back to the form
                dbg["wall"] = "email/approval gate after register (" + gate["url"] + ")"  # flag for the audit
        except Exception:
            pass

        # Return True if we operated the gate (picked the option AND clicked submit) — we reached/passed the
        # form wall, so _capture should NOT run the generic fallback (which can't beat this disabled-button gate).
        # Even if a second email gate exists, returning True is correct: the generic handler would do strictly worse.
        return bool(picked and clicked)
    except Exception as e:
        dbg["platform_err_oe"] = str(e)[:140]                            # surface any unexpected failure for the audit
        return False                                                     # let the generic fallback try on hard failure
