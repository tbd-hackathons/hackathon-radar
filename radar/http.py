"""Polite HTTP: one shared user agent, per-host spacing, retries with backoff."""
from __future__ import annotations

import gzip
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


def _wait_turn(host: str) -> None:
    while True:
        with _lock:
            now = time.time()
            due = _last_hit.get(host, 0) + SLOW.get(host, MIN_GAP)
            if now >= due:
                _last_hit[host] = now
                return
        time.sleep(due - now)


def fetch(url: str, *, data=None, headers=None, timeout=30, retries=2) -> str:
    host = ".".join(urllib.parse.urlparse(url).netloc.split(".")[-2:])  # *.devpost.com share one slot
    body = None
    hdrs = {"User-Agent": UA, "Accept-Encoding": "gzip",
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
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 401, 403, 404, 410):
                break  # retrying will not help
        except Exception as e:  # timeouts, resets
            last = e
        time.sleep(2 ** attempt)
    raise RuntimeError(f"{url}: {last}")


def fetch_json(url: str, **kw):
    body = fetch(url, **kw)
    try:
        return json.loads(body)
    except ValueError:
        raise RuntimeError("got a web page instead of data (bot check or outage); previous data kept")
