"""SQLite store + the scrape pipeline (sources -> score -> read top pages -> rescore)."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import enrich as E
from .match import score
from .sources import NOT_COVERED, SOURCE_NOTES, SOURCES

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "radar.db"
PROFILE = ROOT / "profile.json"
ENRICH_TOP = 80          # pages read per run
ENRICH_TTL = 3 * 86400   # re-read a page after 3 days
PER_SITE_CAP = {"devpost": 20, "aicrowd": 6}  # Devpost bot check; AIcrowd asks 10 s per request
NO_PAGE_READ = ("unstop", "zindi", "hackster")  # Zindi rules come from its API; Hackster pages 403

_lock = threading.Lock()
STATUS = {"running": False, "phase": "idle", "log": [], "last_run": None}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def conn():
    DB.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript("""
    create table if not exists hackathons(id text primary key, source text, data text,
        first_seen text, last_seen text);
    create table if not exists enrich(id text primary key, data text, fetched_at real);
    create table if not exists marks(id text primary key, status text, note text, updated_at text);
    create table if not exists runs(id integer primary key autoincrement, started text,
        finished text, stats text);
    """)
    return c


def load_profile() -> dict:
    return json.loads(PROFILE.read_text())


def _log(msg):
    STATUS["log"] = (STATUS["log"] + [f"{time.strftime('%H:%M:%S')} {msg}"])[-60:]
    print(msg, flush=True)


def _listing_text(h):
    """Text the eligibility patterns read from the listing itself (no page fetch)."""
    return f"{h.get('description') or ''} {h.get('rules_text') or ''}".strip()


def all_scored(include_stale=False):
    profile = load_profile()
    c = conn()
    last = c.execute("select started from runs where finished is not null order by id desc limit 1").fetchone()
    last_started = last["started"] if last else None
    n_runs = c.execute("select count(*) from runs where finished is not null").fetchone()[0]
    enr = {r["id"]: json.loads(r["data"]) for r in c.execute("select id, data from enrich")}
    marks = {r["id"]: dict(r) for r in c.execute("select * from marks")}
    out = []
    for r in c.execute("select * from hackathons"):
        if not include_stale and last_started and r["last_seen"] < last_started:
            continue  # no longer listed by its source
        h = json.loads(r["data"])
        e = E.merge(E.analyse(_listing_text(h), profile["home_country"]), enr.get(h["id"]))
        s = score(h, profile, e)
        if s["days_left"] is not None and s["days_left"] < -1:
            continue
        h.update(s)
        h["evidence"] = e.get("evidence", [])
        h["page_read"] = h["id"] in enr and (enr[h["id"]].get("chars") or 0) > 500
        h["page_blocked"] = bool((enr.get(h["id"]) or {}).get("error"))
        h["first_seen"] = r["first_seen"]
        h["is_new"] = bool(n_runs > 1 and r["first_seen"] >= last_started)
        h["mark"] = (marks.get(h["id"]) or {}).get("status")
        h["summary"] = (h.pop("description", None) or "")[:400]
        h.pop("rules_text", None)
        out.append(h)
    c.close()
    out = _merge_translations(out)
    out.sort(key=lambda x: (x["tier"] != "blocked", x["score"]), reverse=True)
    return out


def _merge_translations(rows):
    """One row per event: some sources list the same event once per language
    (same source, deadline and prize; the title differs after the first ':')."""
    def key(h):
        head = re.split(r"[:：|]", h.get("title") or "", maxsplit=1)[0].strip().lower()
        return (h.get("source"), (h.get("deadline") or "")[:10], h.get("prize_usd"), head) if h.get("deadline") else (h["id"],)
    groups = {}
    for h in rows:
        groups.setdefault(key(h), []).append(h)
    out = []
    for g in groups.values():
        # Only translations merge: a non-ASCII title folds into an ASCII (usually English) one.
        # Two ASCII titles (separate tracks, or generic names like "Hackathon") stay apart.
        ascii_rows = [h for h in g if (h.get("title") or "").isascii()]
        other = [h for h in g if h not in ascii_rows]
        keep_rows = ascii_rows or other[:1]
        extras = other if ascii_rows else other[1:]
        if extras:
            host = next((h for h in keep_rows if h.get("mark")), keep_rows[0])
            host["also_listed_as"] = [{"title": h["title"], "url": h.get("url")} for h in extras]
        out.extend(keep_rows)
    return out


def run_scrape(enrich_top=ENRICH_TOP):
    if not _lock.acquire(blocking=False):
        return False
    try:
        STATUS.update(running=True, phase="sources", log=[])
        started = now()
        c = conn()
        rid = c.execute("insert into runs(started) values(?)", (started,)).lastrowid
        c.commit()
        stats = {}

        def one(name):
            t = time.time()
            try:
                return name, SOURCES[name](), None, time.time() - t
            except Exception as e:
                return name, [], repr(e)[:300], time.time() - t

        with ThreadPoolExecutor(len(SOURCES)) as ex:
            for name, rows, err, dt in ex.map(one, SOURCES):
                exp = getattr(rows, "expected", None)
                stats[name] = {"count": len(rows), "expected": exp, "error": err, "secs": round(dt, 1),
                               "note": getattr(rows, "note", None)}
                _log(f"{name}: {len(rows)} events{f' of {exp} reported' if exp is not None else ''}"
                     f"{' ERROR ' + err if err else ''} ({dt:.1f}s)")
                for h in rows:
                    c.execute("""insert into hackathons(id, source, data, first_seen, last_seen)
                        values(?,?,?,?,?) on conflict(id) do update set data=excluded.data,
                        last_seen=excluded.last_seen""",
                              (h["id"], h["source"], json.dumps(h), started, started))
                c.commit()

        # Sources with an error keep their previous rows visible this run.
        for name, st in stats.items():
            if st["error"]:
                c.execute("update hackathons set last_seen=? where source=?", (started, name))
        c.commit()

        STATUS["phase"] = "reading pages"
        profile = load_profile()
        fresh = {r["id"] for r in c.execute(
            "select id from enrich where fetched_at > ? and json_extract(data, '$.error') is null",
            (time.time() - ENRICH_TTL,))}
        rows = [json.loads(r["data"]) for r in c.execute("select data from hackathons where last_seen=?", (started,))]
        cands = [h for h in rows if h["id"] not in fresh and h.get("url")]
        sc = {h["id"]: score(h, profile, E.analyse(_listing_text(h), profile["home_country"])) for h in cands}
        cands = sorted([h for h in cands if not sc[h["id"]]["blockers"] and h["source"] not in NO_PAGE_READ],
                       key=lambda h: -sc[h["id"]]["score"])
        per, picked = {}, []
        for h in cands:
            cap = PER_SITE_CAP.get(h["source"])
            if cap is not None and per.get(h["source"], 0) >= cap:
                continue
            per[h["source"]] = per.get(h["source"], 0) + 1
            picked.append(h)
        cands = picked[:enrich_top]
        _log(f"reading {len(cands)} hackathon pages for eligibility, AI rules, countries")
        done = 0

        def read(h):
            return h["id"], E.enrich_one(h, profile["home_country"])

        with ThreadPoolExecutor(6) as ex:
            for hid, res in ex.map(read, cands):
                c.execute("insert or replace into enrich(id, data, fetched_at) values(?,?,?)",
                          (hid, json.dumps(res), time.time()))
                done += 1
                if done % 10 == 0:
                    c.commit(); _log(f"read {done}/{len(cands)} pages")
        c.commit()
        stats["_pages_read"] = done
        c.execute("update runs set finished=?, stats=? where id=?", (now(), json.dumps(stats), rid))
        c.commit(); c.close()
        STATUS["last_run"] = {"started": started, "finished": now(), "stats": stats}
        _log("done")
        return True
    finally:
        STATUS.update(running=False, phase="idle")
        _lock.release()


def last_run():
    c = conn()
    r = c.execute("select * from runs where finished is not null order by id desc limit 1").fetchone()
    c.close()
    return dict(r, stats=json.loads(r["stats"])) if r else None


BLOCKED_HINTS = ("403", "bot check", "captcha", "web page instead of data", "429")


def coverage(history=30):
    """Per source: collected vs the source's own total, status, last good run."""
    c = conn()
    runs = [dict(r, stats=json.loads(r["stats"] or "{}")) for r in c.execute(
        "select * from runs where finished is not null order by id desc limit ?", (history,))]
    c.close()
    latest = runs[0] if runs else None
    rows = []
    for name in SOURCES:
        st = (latest or {}).get("stats", {}).get(name) if latest else None
        last_ok = next((r["finished"] for r in runs if (r["stats"].get(name) or {}).get("count") is not None
                        and not r["stats"][name].get("error")), None)
        row = {"source": name, "covered": True, "collected": None, "expected": None, "pct": None,
               "error": None, "secs": None, "run_at": latest["finished"] if latest else None,
               "last_ok": last_ok, "note": SOURCE_NOTES.get(name, "")}
        if st is None:
            row["status"] = "not run yet"
        else:
            row.update(collected=st.get("count"), expected=st.get("expected"), error=st.get("error"),
                       secs=st.get("secs"))
            if st.get("note"):
                row["note"] = (row["note"] + " " + st["note"].capitalize() + ".").strip()
            if row["expected"]:
                row["pct"] = round(100 * row["collected"] / row["expected"])
            if st.get("error"):
                row["status"] = "blocked-by-site" if any(k in st["error"].lower() for k in BLOCKED_HINTS) else "failed"
            elif row["expected"] is not None and row["collected"] < row["expected"]:
                row["status"] = "partial"
            else:
                row["status"] = "ok"
        rows.append(row)
    for nc in NOT_COVERED:
        rows.append({"source": nc["source"], "covered": False, "status": "not covered", "collected": None,
                     "expected": None, "pct": None, "error": None, "secs": None, "run_at": None,
                     "last_ok": None, "note": nc["reason"]})
    return {"rows": rows, "last_run": latest["finished"] if latest else None,
            "pages_read": (latest or {}).get("stats", {}).get("_pages_read") if latest else None}


def set_mark(hid: str, status: str | None, note: str = ""):
    c = conn()
    if status:
        c.execute("insert or replace into marks(id, status, note, updated_at) values(?,?,?,?)",
                  (hid, status, note, now()))
    else:
        c.execute("delete from marks where id=?", (hid,))
    c.commit(); c.close()
