"""Polite HTTP: one shared user agent, per-host spacing, retries with backoff."""
from __future__ import annotations

import gzip
import http.client
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# Say who we are, honestly, with a link a site owner can use to see what this is or ask us to stop.
UA = os.environ.get("RADAR_USER_AGENT",
                    "HackathonRadar/1.0 (+https://github.com/tbd-hackathons/hackathon-radar)")

# Per-host exceptions to the honest user agent above. Only for sites that answer
# our honest UA with a bot wall (HTTP 405/403) yet serve the very same public data
# to a browser UA. Named per host so the exception is visible, not silent; the
# README "Limits" section discloses which hosts are listed and why.
HOST_UA = {
    "dorahacks.io": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36",
}
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


def _auto_source_ip():
    """Address of a physical interface, for routing around a VPN exit a site blocks.

    Best effort: no dependency, no privileges, no change unless a request is refused.
    None means "no idea", and the caller then behaves exactly as it did before.
    """
    if sys.platform == "darwin":
        for iface in ("en0", "en1", "en2"):
            try:
                r = subprocess.run(["ipconfig", "getifaddr", iface],
                                   capture_output=True, text=True, timeout=5)
            except Exception:
                continue
            if r.returncode == 0 and (r.stdout or "").strip():
                return r.stdout.strip()
        return None
    try:  # Linux and friends
        r = subprocess.run(["ip", "route", "get", "1.1.1.1"],
                           capture_output=True, text=True, timeout=5)
        m = re.search(r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)", r.stdout or "")
        return m.group(1) if m else None
    except Exception:
        return None


_OPENER = _build_opener(SOURCE_IP)
_ACTIVE = {"opener": _OPENER}
_auto_lock = threading.Lock()
_auto_state = {"done": False}


def _reroute_around_block() -> bool:
    """After a 403/405, bind to the physical interface and keep using it. Once only.

    This is what makes a host that rejects a VPN/datacenter IP work with no env var
    and no user-agent lie. If detection finds nothing, nothing changes.
    """
    with _auto_lock:
        if _auto_state["done"]:
            return False
        _auto_state["done"] = True
        ip = _auto_source_ip()
        if not ip:
            return False
        _ACTIVE["opener"] = _build_opener(ip)
        print(f"http: a host refused our IP; routing via {ip} from now on", flush=True)
        return True


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
    hdrs = {"User-Agent": HOST_UA.get(host, UA), "Accept-Encoding": "gzip",
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8"}
    if data is not None:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    last = None
    for attempt in range(retries + 1):
        _wait_turn(host)
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs)
            op = _ACTIVE["opener"].open if _ACTIVE["opener"] else urllib.request.urlopen
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
            # A 403/405 is often an IP wall rather than a bad request. Reroute, then retry if
            # the route really did change under us. Compare openers rather than trusting a
            # flag: another thread may have switched routes while this request was in flight,
            # in which case this one still went out the old way and deserves its retry.
            if e.code in (403, 405) and SOURCE_IP is None:
                _reroute_around_block()
                now = _ACTIVE["opener"].open if _ACTIVE["opener"] else urllib.request.urlopen
                if now is not op:
                    continue
            if e.code in (400, 401, 403, 404, 410):
                break  # retrying will not help
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
