"""Polite HTTP: one shared user agent, per-host spacing, retries with backoff."""
from __future__ import annotations

import gzip
import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# Say who we are, honestly, with a link a site owner can use to see what this is or ask us to stop.
UA = os.environ.get("RADAR_USER_AGENT",
                    "HackathonRadar/1.0 (+https://github.com/tbd-hackathons/hackathon-radar)")

MIN_GAP = 1.0  # seconds between two requests to the same site
SLOW = {"devpost.com": 3.0, "aicrowd.com": 10.0}  # rate-limit harder; aicrowd robots.txt Crawl-delay: 10

_last_hit: dict = {}
_lock = threading.Lock()

# Optional: bind outgoing sockets to one local IP. Some networks (a VPN exit node, a
# hotspot) send our traffic out through an IP a site blocks as "anonymous". Binding to
# a physical interface's address routes around it without lying in the user agent.
#   RADAR_SOURCE_IP=$(ipconfig getifaddr en0) python3 radar.py scrape   # macOS
SOURCE_IP = os.environ.get("RADAR_SOURCE_IP") or None


def _build_opener(ip):
    """A urllib opener that binds outgoing sockets to `ip`, or None when ip is empty."""
    if not ip:
        return None

    class _HTTP(http.client.HTTPConnection):
        def __init__(self, *a, **kw):
            kw.setdefault("source_address", (ip, 0))
            super().__init__(*a, **kw)

    class _HTTPS(http.client.HTTPSConnection):
        def __init__(self, *a, **kw):
            kw.setdefault("source_address", (ip, 0))
            super().__init__(*a, **kw)

    class _Handler(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
        def http_open(self, req):
            return self.do_open(_HTTP, req)

        def https_open(self, req):
            return self.do_open(_HTTPS, req)

    return urllib.request.build_opener(_Handler)


_OPENER = _build_opener(SOURCE_IP)


def _wait_turn(host: str) -> None:
    while True:
        with _lock:
            now = time.time()
            due = _last_hit.get(host, 0) + SLOW.get(host, MIN_GAP)
            if now >= due:
                _last_hit[host] = now
                return
        time.sleep(due - now)


def fetch_headers(url: str, *, data=None, headers=None, timeout=30, retries=2):
    """Like fetch(), but returns (body, headers). Headers are lower-cased, so a caller
    can read Set-Cookie (e.g. Eventbrite's CSRF token, which is not in the page body)."""
    host = ".".join(urllib.parse.urlparse(url).netloc.split(".")[-2:])  # *.devpost.com share one slot
    body = None
    hdrs = {"User-Agent": UA, "Accept-Encoding": "gzip",
            # Keep this simple. Devpost's WAF answers 403 to any Accept that carries q-values
            # (its subdomains refused our rules pages for exactly that reason), and */* still
            # lets every JSON API return JSON.
            "Accept": "*/*"}
    if data is not None:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    last = None
    for attempt in range(retries + 1):
        _wait_turn(host)
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs)
            op = _OPENER.open if _OPENER else urllib.request.urlopen
            with op(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                out = {}
                for k, v in r.headers.items():  # keep every Set-Cookie, not just the last
                    k = k.lower()
                    out[k] = f"{out[k]}, {v}" if k in out else v
                return raw.decode("utf-8", "replace"), out
        except urllib.error.HTTPError as e:
            last = e
            # A refusal is the site's answer. Respect it: no retry, no other route, no other identity.
            if e.code in (400, 401, 403, 404, 405, 410):
                break
        except Exception as e:  # timeouts, resets
            last = e
        time.sleep(2 ** attempt)
    raise RuntimeError(f"{url}: {last}")


def fetch(url: str, **kw) -> str:
    return fetch_headers(url, **kw)[0]


def fetch_json(url: str, **kw):
    body = fetch(url, **kw)
    try:
        return json.loads(body)
    except ValueError:
        raise RuntimeError("got a web page instead of data (bot check or outage); previous data kept")
