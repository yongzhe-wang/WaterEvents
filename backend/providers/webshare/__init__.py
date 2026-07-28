"""Webshare residential-proxy provider — routes watercrawl's fetches (curl_cffi impersonate + headless Chromium) through
Webshare's ROTATING RESIDENTIAL IPs so the datacenter-blocked IR sites that return 0 links from Cloud Run's datacenter
egress (TEPCO 9501.T, Wanhai 2615.TW, many .jp/.tw/.kr) get the REAL page.

用一句话讲完: Cloud Run 的 datacenter IP 被这些站整个封(headless + curl_cffi 都拿到 0 links,实测 TEPCO impersonate=0/pool=0)
→ 走 Webshare 的住宅出口 IP,目标站看到的是住宅 IP 不是 datacenter → 封锁不适用 → 拿到真页。这是 datacenter-block 唯一的正解。

WHY Webshare (not the deleted self-hosted ngrok wall_relay): the old relay tunneled to a home Mac via ngrok — a single
residential IP, fragile tunnel, manual to run. Webshare is a managed residential-proxy pool: one endpoint, per-request IP
rotation, no box to babysit. {USER 2026-07-19 "delete those code it is not relay ... should be webshare"}.

Config (env; from Secret Manager in prod). SWITCHED 2026-07-22 to Webshare's ROTATING PROXY ENDPOINT: a SINGLE gateway
`p.webshare.io:80` that assigns a fresh residential IP server-side PER REQUEST (username carries the `-rotate` suffix).
So WEBSHARE_PROXIES now holds just that one gateway host and proxy_url() no longer rotates a list itself — the gateway
does. WHY the switch: the old STATIC-RESIDENTIAL list (fixed `ip:port` endpoints) hit per-IP `403/407 CONNECT tunnel`
failures under load (individual IPs deauth/rate-limit), so a fetch that landed on a bad IP failed; the rotating gateway
gives a fresh IP every call and never pins a dead one. {USER 2026-07-22 "change this to rotate so it is always working";
TESTED on Cloud Run: rotating ipify 200 with IPs 212.212.18.184→194.113.81.244→46.202.3.61 rotating, static IPs 403/407}
[CONFIDENCE: CONFIRMED — GCP wc-test proved the rotating gateway 200s + rotates while the static list 403/407'd].
  WEBSHARE_USERNAME  — the rotating gateway username (the `<user>-rotate` variant)
  WEBSHARE_PASSWORD  — the proxy password (shared with the static plan; unchanged)
  WEBSHARE_PROXIES   — the rotating gateway host `p.webshare.io:80` (a single entry; the gateway rotates IPs server-side)
  WEBSHARE_PROXY     — OR a single full proxy url override (http://user:pass@host:port) — wins if set
Unset → enabled()=False and every accessor returns empty/None, so watercrawl runs EXACTLY as before (feature dormant).
NOTE: a fingerprint-based wall (raymondjames RJF 403s curl_cffi from EVERY residential IP + datacenter) is NOT fixed by
rotating — that needs a real browser (patchright FB3) or is a genuine $0 wall. Rotating fixes the per-IP infra failures,
not target-side fingerprint blocks. {WEBSHARE 2026 rotating-endpoint quickstart "p.webshare.io:80 <user>-rotate"}.
"""
import os
import random
from urllib.parse import quote, urlparse

_USER = os.environ.get("WEBSHARE_USERNAME", "").strip()
_PW = os.environ.get("WEBSHARE_PASSWORD", "").strip()
# the static-residential endpoint list: "ip:port,ip:port,..." → [(ip,port), ...]. Whitespace/blank-tolerant.
_PROXIES = [p.strip() for p in os.environ.get("WEBSHARE_PROXIES", "").replace(";", ",").split(",") if p.strip()]


def proxy_url() -> str:
    """An http(s) proxy URL for curl_cffi / requests — the Webshare ROTATING GATEWAY (`<user>-rotate@p.webshare.io:80`,
    which assigns a fresh residential IP server-side per request), or '' when Webshare is unconfigured (feature dormant).
    WEBSHARE_PROXIES now holds the single gateway host, so random.choice returns it every call and the GATEWAY does the
    IP rotation — no per-IP list to hit a deauth'd/403'd endpoint. (List-of-N still works if ops ever repopulates it.)"""
    direct = os.environ.get("WEBSHARE_PROXY", "").strip()   # a pre-built full url wins (lets ops pin one endpoint)
    if direct:
        return direct
    if not (_USER and _PW and _PROXIES):                    # need creds AND the gateway host → else dormant
        return ""
    ipport = random.choice(_PROXIES)                        # normally the single rotating gateway; a random one if a list remains
    return f"http://{quote(_USER)}:{quote(_PW)}@{ipport}"


def enabled() -> bool:
    """True when Webshare creds are present → watercrawl should route through the residential proxy."""
    return bool(proxy_url())


def curl_proxies() -> dict:
    """{'http': url, 'https': url} for curl_cffi's `proxies=`, or {} when dormant (curl_cffi then goes direct)."""
    url = proxy_url()
    return {"http": url, "https": url} if url else {}


def playwright_proxy() -> dict | None:
    """Playwright/Chromium launch proxy dict {server, username, password}, or None when dormant. Chromium takes the
    auth separately from the server url, so we split the curl-style url apart here. {PLAYWRIGHT proxy launch option}."""
    url = proxy_url()
    if not url:
        return None
    p = urlparse(url)
    return {"server": f"http://{p.hostname}:{p.port or 80}",
            "username": p.username or "", "password": p.password or ""}
