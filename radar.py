#!/usr/bin/env python3
"""Hackathon Radar — find hackathons worldwide that fit Fury + the AI team.

  python3 radar.py serve [--port 8787]   UI on http://127.0.0.1:8787
  python3 radar.py scrape                one full run from the terminal
  python3 radar.py top [N]               print the best N matches
"""
from __future__ import annotations

import csv
import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from radar import store
from radar.match import score  # noqa: F401  (handy in a REPL)

STATIC = Path(__file__).resolve().parent / "static"
PIPELINE = Path(__file__).resolve().parent / "pipeline.json"  # your own progress: kept out of git


def read_pipeline() -> dict:
    """Your pipeline, or an empty one on a fresh checkout (pipeline.example.json shows the shape)."""
    if PIPELINE.exists():
        return json.loads(PIPELINE.read_text())
    return {"goal_usd": 0, "goal_date": None, "won_usd": 0, "entries": []}
STAGES = ["watching", "shortlisted", "registered", "building", "submitted", "won", "lost"]


def pipeline():
    """Hackathons we're in, joined with live radar data; shortlist/applied marks join automatically."""
    p = read_pipeline()
    live = {h["id"]: h for h in store.all_scored(include_stale=True)}
    ids = {e["id"] for e in p["entries"]}
    for h in live.values():
        if h.get("mark") in ("shortlist", "applied") and h["id"] not in ids:
            p["entries"].append({"id": h["id"], "title": h["title"], "url": h["url"],
                                 "stage": "registered" if h["mark"] == "applied" else "shortlisted",
                                 "deadline": h.get("deadline"), "top_prize_usd": None,
                                 "next_step": "Read the rules and decide", "note": "", "auto": True})
    for e in p["entries"]:
        h = live.get(e["id"])
        if h:
            e.update(pool_usd=h.get("prize_usd"), score=h["score"], tier=h["tier"],
                     format=h.get("format"), deadline=e.get("deadline") or h.get("deadline"))
    p["stages"] = STAGES
    return p


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if not isinstance(body, (bytes, str)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html")
        if u.path == "/api/hackathons":
            return self._send(200, {"items": store.all_scored(), "last_run": store.last_run(),
                                    "status": store.STATUS})
        if u.path == "/api/pipeline":
            return self._send(200, pipeline())
        if u.path == "/api/coverage":
            return self._send(200, store.coverage())
        if u.path == "/api/status":
            return self._send(200, store.STATUS)
        if u.path == "/api/profile":
            return self._send(200, store.load_profile())
        if u.path == "/api/export.csv":
            q = parse_qs(u.query)
            tiers = set((q.get("tiers") or ["go,maybe"])[0].split(","))
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["score", "tier", "title", "source", "format", "location", "deadline",
                        "prize_usd", "why", "blockers", "url"])
            for h in store.all_scored():
                if h["tier"] in tiers or h.get("mark") == "shortlist":
                    w.writerow([h["score"], h["tier"], h["title"], h["source"], h["format"],
                                h.get("location"), h.get("deadline"), h.get("prize_usd"),
                                " | ".join(r["why"] for r in h["reasons"]), ",".join(h["blockers"]), h["url"]])
            return self._send(200, buf.getvalue(), "text/csv")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/refresh":
            if store.STATUS["running"]:
                return self._send(409, {"error": "already running"})
            threading.Thread(target=store.run_scrape, daemon=True).start()
            return self._send(202, {"ok": True})
        if u.path == "/api/mark":
            b = self._body()
            store.set_mark(b["id"], b.get("status"), b.get("note", ""))
            return self._send(200, {"ok": True})
        if u.path == "/api/pipeline":
            b = self._body()
            if b.get("stage") not in STAGES:
                return self._send(400, {"error": "unknown stage"})
            p = read_pipeline()
            e = next((x for x in p["entries"] if x["id"] == b["id"]), None)
            if e is None:
                e = {k: b.get(k) for k in ("id", "title", "url", "deadline")}
                e.update(top_prize_usd=None, next_step="", note="")
                p["entries"].append(e)
            e["stage"] = b["stage"]
            if "next_step" in b:
                e["next_step"] = b["next_step"]
            PIPELINE.write_text(json.dumps(p, indent=2, ensure_ascii=False) + "\n")
            return self._send(200, {"ok": True})
        if u.path == "/api/profile":
            b = self._body()
            for k in ("home_country", "themes", "blockers", "travel_ok_countries"):
                if k not in b:
                    return self._send(400, {"error": f"profile needs {k}"})
            store.PROFILE.write_text(json.dumps(b, indent=2, ensure_ascii=False))
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "serve"
    if cmd == "scrape":
        store.run_scrape()
    elif cmd == "top":
        n = int(argv[2]) if len(argv) > 2 else 20
        for h in [x for x in store.all_scored() if x["tier"] != "blocked"][:n]:
            print(f"{h['score']:3d} {h['tier']:6} {h['title'][:60]:60} {h['format']:9} "
                  f"{(h.get('deadline') or '')[:10]}  {h['url']}")
    elif cmd == "serve":
        port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 8787
        store.conn().close()
        if store.last_run() is None:
            threading.Thread(target=store.run_scrape, daemon=True).start()
        print(f"Hackathon Radar on http://127.0.0.1:{port}")
        ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
