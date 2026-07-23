"""KnowledgeVision (kvgo.com) webcast register handler.

用一句话讲完: kvgo 的 event page 只是个 thin shell,真正的 registration 表单藏在一个晚加载的
view.knowledgevision.com iframe 里 → 我们先轮询等这个 iframe 出现(~22s 预算)→ 再在 frame 上
wait_for_selector('input') 等 SPA 把 4 个字段 render 出来 → 按 id 填(#Name/#Email/#Company/
#Job_Title)→ 点 'Register' submit,submit 后 player 自己加载标准 HLS。

The event page is a thin shell that embeds the real registration form + player in a
view.knowledgevision.com IFRAME. The generic register races the iframe (it renders ~3.5s in) and
misses the fields. This handler waits for the iframe form, fills it (id/name selectors as a fallback
to label/placeholder), submits, and lets the player load its HLS. {WORKFLOW could-not-fetch audit:
kvgo register filled=[] clicked=None → 0 m3u8; verifier reached the player via explicit waits + id
selectors} [CONFIDENCE: SINGLE-SRC — diagnosed, implementation verified by the platform workflow]
"""
from __future__ import annotations  # async-native module — defer annotation eval, keep sync-port shape

NAME = "kvgo"


def matches(url: str) -> bool:
    # host-based dispatch — kvgo.com 或 knowledgevision 都路由到本 handler(逻辑与 sync 版一字不差)
    u = url or ""
    return "kvgo.com" in u or "knowledgevision" in u


async def register(page, frames, reg, dbg, fill_in) -> bool:
    """Reach the KnowledgeVision player past its iframe registration form. Return True once we
    submit the form (so _capture.py skips the generic fallback that races the late iframe).

    WHY: the kvgo.com event page is a thin shell; the real registration form lives in a
    view.knowledgevision.com IFRAME whose <iframe src> is only set ~3s in and whose form fields
    render LATER still (SPA). The generic fill races both → filled=[]/clicked=None/0 m3u8. We
    instead (1) poll up to 25s for the view.* frame, (2) wait_for_selector('input') ON that frame so
    the SPA has rendered, (3) fill by id (#Name/#Email/#Company/#Job_Title) with get_by_label as a
    fallback, (4) click the 'Register' submit button. After submit the player loads standard HLS.
    {PROBE /tmp/probe_kvgo3.py 2026-06-14 — view frame INPUTS(4): id='Name'(text) id='Email'(email)
    id='Company'(text) id='Job_Title'(text), all placeholder='' aria='' ; BUTTONS(1):
    type='submit' text='Register' ; LABELS: 'Name *'->Name 'Email Address *'->Email 'Company *'
    ->Company 'Job Title *'->Job_Title ; iframe src set at t=3s, child frame nav at t=4s}
    [CONFIDENCE: CONFIRMED 95% — live-probed the exact ids+labels+button; only the post-submit
    .m3u8 capture remains to be confirmed by the isolation verifier]"""
    import re  # match the KnowledgeVision view-frame host

    # The view frame may not exist yet when the handler runs (it navigates ~t=3-4s); poll for it.
    # _capture already waited 3500ms before calling us, but we re-poll defensively up to ~22s.
    view = None
    for _ in range(22):                                            # ~22s budget for the SPA iframe to appear
        for f in page.frames:                                     # re-read frames each pass — the list grows late (property, 不 await)
            u = f.url or ""                                       # f.url 是 property,同步读取,不 await
            if "knowledgevision" in u or "view." in u:            # the registration/player frame host
                view = f
                break
        if view:
            break
        try:
            await page.wait_for_timeout(1000)                     # 1s between polls; cheap, bounded by the loop (async → await)
        except Exception:
            break
    if view is None:                                              # never found the iframe → let generic try
        dbg["wall"] = "kvgo: view.knowledgevision iframe never appeared"
        return False

    # Wait for the form to actually render inside the frame (the inputs lag the frame nav).
    try:
        await view.wait_for_selector("input", timeout=20000, state="attached")  # SPA renders fields late (async → await)
    except Exception as e:
        dbg["wall"] = "kvgo: iframe present but no input rendered (" + str(e)[:60] + ")"
        return False
    try:
        await page.wait_for_timeout(1200)                        # brief grace so all 4 fields are present (async → await)
    except Exception:
        pass

    # FILL by id first (probe showed id==name==Name/Email/Company/Job_Title, placeholder+aria empty),
    # falling back to the shared fill_in (label-based) when an id selector is missing on a variant page.
    # reg has {first,last,company,email} only — 'Name' is the full name, so join first+last.
    full_name = (reg.get("first", "") + " " + reg.get("last", "")).strip()  # KnowledgeVision wants one Name field
    field_plan = [
        ("#Name", ["Name", "Full Name"], full_name, "name"),               # id #Name / label 'Name *'
        ("#Email", ["Email", "Email Address", "E-mail"], reg.get("email", ""), "email"),  # id #Email / 'Email Address *'
        ("#Company", ["Company", "Organization", "Firm"], reg.get("company", ""), "company"),  # id #Company / 'Company *'
        ("#Job_Title", ["Job Title", "Title", "Role"], "Analyst", "title"),  # id #Job_Title / 'Job Title *'
    ]
    for sel, labels, val, tag in field_plan:
        if not val:                                              # skip empty values (e.g. missing email)
            continue
        done = False
        try:
            loc = view.locator(sel)                             # id selector — the most reliable per probe (locator 同步返回, 不 await)
            if await loc.count() > 0:                           # .count() → await (async API)
                await loc.first.fill(val, timeout=3000)         # fill the field; 3s allows late attach (.fill() → await)
                done = True
        except Exception:
            done = False
        if not done:                                            # id missing on a variant → label/placeholder fallback
            try:
                done = bool(await fill_in(view, labels, val))   # shared helper now async → await fill_in(...)
            except Exception:
                done = False
        if done:
            dbg["filled"].append(tag)                           # record what we managed to fill for the audit

    # SUBMIT — probe found a single <button type=submit> labelled 'Register'. Try role-name first,
    # then a raw submit-button selector as a fallback.
    clicked = False
    for getter, label in (
        (lambda: view.get_by_role("button", name=re.compile("Register", re.I)), "Register"),  # role+name (get_by_role 返回 locator, 不 await)
        (lambda: view.locator("button[type=submit]"), "submit"),                              # raw submit fallback (locator 同步)
    ):
        try:
            btn = getter()
            if await btn.count() > 0:                           # .count() → await (async API)
                await btn.first.click(timeout=3000)             # submit the registration form (.click() → await)
                dbg["clicked"] = label
                clicked = True
                break
        except Exception:
            continue

    if not clicked:                                            # filled but couldn't submit → flag, let generic try
        dbg["wall"] = "kvgo: form filled but Register button not clickable"
        return False

    # Give the player a moment to swap the registration view for the HLS player after submit.
    try:
        await page.wait_for_timeout(2500)                     # post-submit grace; _capture then polls for m3u8 (async → await)
    except Exception:
        pass
    return True                                                # acted: reached/submitted → _capture skips generic
