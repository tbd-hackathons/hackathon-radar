"""Source adapters. Each returns a list of normalized hackathon dicts (see `record`).

Adding a source = one function + one entry in SOURCES. An adapter that fails
raises; the runner records the error per source and keeps the others.
An adapter may return a `Batch` instead of a plain list to also report the
source's own total ("expected"), which the coverage page compares against.
"""
from __future__ import annotations

import html as htmllib
import json
import re
from datetime import datetime, timezone

from .http import fetch, fetch_headers, fetch_json

# Approximate FX to USD, used only to rank prizes. Not for money decisions.
FX = {"USD": 1, "$": 1, "EUR": 1.1, "€": 1.1, "GBP": 1.3, "£": 1.3, "INR": 0.012,
      "₹": 0.012, "RS": 0.012, "IDR": 0.00006, "RP": 0.00006, "SGD": 0.77,
      "CAD": 0.73, "AUD": 0.66, "JPY": 0.0068, "¥": 0.0068, "CHF": 1.15,
      "BRL": 0.18, "R$": 0.18, "USDC": 1, "USDT": 1, "HKD": 0.13, "MYR": 0.23,
      "THB": 0.03, "PHP": 0.018, "VND": 0.00004, "KRW": 0.00072, "NGN": 0.00065,
      "ZAR": 0.055, "SZL": 0.055, "KES": 0.0077}


class Batch(list):
    """Rows from one source, plus what the source itself says it has."""

    def __init__(self, rows=(), expected=None, note=None):
        super().__init__(rows)
        self.expected = expected  # the source's own total, None = not reported
        self.note = note


def record(source, native_id, title, url, **kw):
    r = {
        "id": f"{source}:{native_id}", "source": source, "title": (title or "").strip(),
        "url": url, "organizer": None, "start": None, "end": None, "deadline": None,
        "format": "unknown", "location": None, "country": None, "prize_usd": None,
        "prize_text": None, "themes": [], "description": "", "team_min": None,
        "team_max": None, "participants": None, "flags": {}, "image": None,
    }
    r.update({k: v for k, v in kw.items() if v is not None})
    r["description"] = clean_text(r["description"])[:4000]
    return r


def clean_text(s) -> str:
    if not s:
        return ""
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", str(s), flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = htmllib.unescape(s)
    s = re.sub(r"[*_#`>|]+", " ", s)  # markdown noise
    return re.sub(r"\s+", " ", s).strip()


def iso(dt) -> str | None:
    if not dt:
        return None
    if isinstance(dt, str):
        s = dt.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


MONEY = re.compile(
    r"(?P<cur>US\$|USD|USDC|USDT|EUR|GBP|INR|IDR|SGD|CAD|AUD|CHF|HKD|MYR|ZAR|KES|R\$|Rs\.?|Rp|[$€£₹¥])\s?"
    r"(?P<num>\d[\d,\.]*)\s?(?P<mult>[kKmM](?![a-zA-Z]))?"
    r"|(?P<num2>\d[\d,\.]*)\s?(?P<mult2>[kKmM])?\s?(?P<cur2>USD|USDC|USDT|EUR|INR|IDR|SGD|GBP|ZAR|SZL|KES)\b")


def money_usd(text) -> float | None:
    """Largest single money figure in the text, in USD."""
    best = None
    for m in MONEY.finditer(text or ""):
        cur = (m.group("cur") or m.group("cur2") or "$").upper().rstrip(".")
        cur = "USD" if cur == "US$" else cur
        num = (m.group("num") or m.group("num2") or "").rstrip(".,")
        mult = (m.group("mult") or m.group("mult2") or "").lower()
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        v *= {"k": 1e3, "m": 1e6}.get(mult, 1)
        usd = v * FX.get(cur, FX.get(cur[:3], 1))
        if usd < 5_000_000 and (best is None or usd > best):
            best = usd
    return best


# ---------------------------------------------------------------- Devpost
def _devpost_end(s: str):
    # "Aug 31 - Oct 23, 2026" | "Sep 01 - 30, 2026" | "Dec 15, 2026 - Jan 10, 2027"
    try:
        left, right = [x.strip() for x in s.split(" - ")]
        if not re.match(r"[A-Za-z]", right):
            right = left.split()[0] + " " + right
        end = datetime.strptime(right, "%b %d, %Y")
        if "," not in left:
            left = f"{left}, {end.year}"
        start = datetime.strptime(left, "%b %d, %Y")
        if start > end:
            start = start.replace(year=start.year - 1)
        return iso(start), iso(end.replace(hour=23, minute=59))
    except Exception:
        return None, None


def devpost():
    # The API caps per_page at 40 whatever we ask for, so page by what it returns
    # (meta.per_page) and stop on total_count or an empty page.
    out, seen, page, total = Batch(), set(), 1, None
    while page <= 12:
        d = fetch_json("https://devpost.com/api/hackathons?status[]=upcoming&status[]=open"
                       f"&per_page=50&page={page}")
        total = d["meta"]["total_count"]
        if not d["hackathons"]:
            break
        for h in d["hackathons"]:
            if h["id"] in seen:
                continue
            seen.add(h["id"])
            start, end = _devpost_end(h.get("submission_period_dates") or "")
            loc = (h.get("displayed_location") or {}).get("location") or ""
            online = loc.lower() == "online"
            prize_text = clean_text(h.get("prize_amount"))
            out.append(record(
                "devpost", h["id"], h["title"], h["url"],
                organizer=h.get("organization_name"), start=start, end=end, deadline=end,
                format="online" if online else "in-person", location=loc,
                prize_usd=money_usd(prize_text), prize_text=prize_text,
                themes=[t["name"] for t in h.get("themes") or []],
                participants=h.get("registrations_count"),
                image=("https:" + h["thumbnail_url"]) if str(h.get("thumbnail_url", "")).startswith("//") else h.get("thumbnail_url"),
                flags={"invite_only": bool(h.get("invite_only"))},
            ))
        if len(seen) >= total or page * (d["meta"].get("per_page") or 40) >= total:
            break
        page += 1
    out.expected = total
    return out


# ---------------------------------------------------------------- Devfolio
def devfolio():
    out, seen = Batch(expected=0), set()
    for kind in ("application_open", "upcoming"):
        d = fetch_json("https://api.devfolio.co/api/search/hackathons",
                       data={"type": kind, "from": 0, "size": 100})
        out.expected += ((d["hits"].get("total") or {}).get("value") or 0)
        for hit in d["hits"].get("hits", []):
            h = hit["_source"]
            if h["uuid"] in seen:
                continue
            seen.add(h["uuid"])
            st = h.get("hackathon_setting") or {}
            prizes = " ".join(f"{p.get('name','')} {p.get('desc','')}" for p in h.get("prizes") or [])
            loc = "Online" if h.get("is_online") else ", ".join(
                x for x in (h.get("city"), h.get("country")) if x)
            out.append(record(
                "devfolio", h["uuid"], h["name"], f"https://{h['slug']}.devfolio.co/",
                organizer=h.get("hosted_by") or None, start=iso(h.get("starts_at")),
                end=iso(h.get("ends_at")), deadline=iso(st.get("reg_ends_at")) or iso(h.get("starts_at")),
                format="online" if h.get("is_online") else "in-person", location=loc,
                country=h.get("country"), prize_usd=money_usd(prizes),
                prize_text=clean_text(prizes)[:200] or None,
                themes=[t.get("name", "") for t in h.get("themes") or []] + list(h.get("hashtags") or []),
                description=f"{h.get('tagline') or ''}. {h.get('desc') or ''}",
                team_min=h.get("team_min"), team_max=h.get("team_size"),
                participants=h.get("participants_count"), image=h.get("cover_img"),
                flags={"women_only": bool(st.get("women_only")), "invite_only": bool(h.get("private"))},
            ))
    return out


# ---------------------------------------------------------------- Unstop
def unstop():
    out, page, last = Batch(), 1, 1
    while page <= last and page <= 10:
        d = fetch_json("https://unstop.com/api/public/opportunity/search-result?opportunity=hackathons"
                       f"&per_page=50&page={page}&oppstatus=open")["data"]
        last = d.get("last_page") or 1
        out.expected = d.get("total")
        for h in d["data"]:
            rr = h.get("regnRequirements") or {}
            cash, cur = 0.0, "INR"
            for p in h.get("prizes") or []:
                if p.get("cash"):
                    cur = "USD" if "dollar" in str(p.get("currency")) else "INR"
                    cash += float(p["cash"])
            elig = rr.get("eligibility") or ""
            filt = [f["name"] for f in h.get("filters") or []]
            addr = h.get("address_with_country_logo") or {}
            online = h.get("region") == "online"
            try:
                sector = set(json.loads(elig).get("sector") or []) if elig else set()
            except ValueError:
                sector = set()
            students = sector == {"students"} or (
                filt and all(re.search(r"student|graduate|engineering|college|school", f, re.I)
                             for f in filt if f))
            out.append(record(
                "unstop", h["id"], h["title"], h.get("seo_url"),
                organizer=(h.get("organisation") or {}).get("name"),
                start=iso(h.get("start_date")), end=iso(h.get("end_date")),
                deadline=iso(rr.get("end_regn_dt")) or iso(h.get("end_date")),
                format="online" if online else "in-person",
                location="Online" if online else ", ".join(x for x in (addr.get("city"), addr.get("state")) if x) or "India",
                country=None if online else "India",
                prize_usd=cash * FX[cur] if cash else None,
                prize_text=f"{cur} {cash:,.0f}" if cash else None,
                themes=[x for x in [t.get("skill") for t in (h.get("required_skills") or []) if isinstance(t, dict)]
                       + [w.get("name") for w in h.get("workfunction") or []] if x],
                description=h.get("details"), team_min=rr.get("min_team_size"),
                team_max=rr.get("max_team_size"), image=h.get("logoUrl2"),
                flags={"students_only": bool(students), "paid_entry": bool(h.get("isPaid")), "india_platform": True,
                       "eligibility": ", ".join(filt)},
            ))
        page += 1
    return out


# ---------------------------------------------------------------- MLH
def mlh():
    s = fetch("https://www.mlh.com/seasons/2027/events")
    m = re.search(r'data-page="app" type="application/json">(.*?)</script>', s, re.S)
    d = json.loads(m.group(1))["props"]
    out = []
    for e in d.get("upcomingEvents") or []:
        va = e.get("venueAddress") or {}
        fmt = {"physical": "in-person", "digital": "online", "hybrid": "hybrid"}.get(e.get("formatType"), "unknown")
        out.append(record(
            "mlh", e["id"], e["name"], e.get("websiteUrl") or f"https://www.mlh.com{e.get('url','')}",
            organizer="MLH member event", start=iso(e.get("startsAt")), end=iso(e.get("endsAt")),
            deadline=iso(e.get("startsAt")), format=fmt, location=e.get("location"),
            country=va.get("country"), image=e.get("logoUrl"),
            themes=list((e.get("customFields") or {}).get("hackathon_focus") or []),
            flags={"students_only": True, "note": "MLH member events are student hackathons"},
        ))
    return out


# ---------------------------------------------------------------- lablab.ai
def _rsc(page: str) -> str:
    out = []
    for m in re.finditer(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', page, re.S):
        try:
            out.append(json.loads('"' + m.group(1) + '"'))
        except ValueError:
            pass
    return "".join(out)


def _objects_with(blob: str, key: str, must=("slug",), back=12000):
    """Recover JSON objects embedded in a React flight payload that contain `key`."""
    dec = json.JSONDecoder()
    found, done = [], set()
    for m in re.finditer(re.escape(f'"{key}":'), blob):
        pos = m.start()
        lo = max(0, pos - back)
        i = blob.rfind("{", lo, pos)
        while i >= lo:
            try:
                obj, end = dec.raw_decode(blob, i)
                if end > pos and isinstance(obj, dict) and key in obj and all(k in obj for k in must):
                    if i not in done:
                        done.add(i)
                        found.append(obj)
                    break
            except ValueError:
                pass
            i = blob.rfind("{", lo, i)
    return found


def lablab():
    blob = _rsc(fetch("https://lablab.ai/ai-hackathons"))
    out, seen = [], set()
    now = datetime.now(timezone.utc)
    for e in _objects_with(blob, "startAt", must=("slug", "endAt")):
        if e.get("type") not in (None, "HACKATHON") or e["slug"] in seen:
            continue
        seen.add(e["slug"])
        end = iso(e.get("endAt"))
        if not end or datetime.fromisoformat(end.replace("Z", "+00:00")) < now:
            continue
        et = (e.get("eventType") or "").upper()
        fmt = {"ONLINE": "online", "HYBRID": "hybrid", "ONSITE": "in-person", "OFFLINE": "in-person"}.get(et, "online")
        prize_blob = json.dumps({k: v for k, v in e.items() if "prize" in k.lower()})
        out.append(record(
            "lablab", e.get("id") or e["slug"], e.get("name") or e.get("title") or e["slug"],
            f"https://lablab.ai/ai-hackathons/{e['slug']}", organizer="lablab.ai",
            start=iso(e.get("startAt")), end=end,
            deadline=end if e.get("signupActive") else iso(e.get("startAt")),
            format=fmt, location=e.get("location") or ("Online" if fmt == "online" else None),
            prize_usd=money_usd(prize_blob), image=e.get("thumbnailLink"),
            description=" ".join(str(e.get(k) or "") for k in ("subtitle", "description", "tech")),
            themes=["AI"] + [t.get("name", "") if isinstance(t, dict) else str(t) for t in (e.get("technologyList") or [])][:12], team_min=e.get("teamMembersMinimum"), team_max=e.get("teamMembersLimit"),
            flags={"signup_open": bool(e.get("signupActive"))},
        ))
    return out


# ---------------------------------------------------------------- ETHGlobal
def _eth_loc(e):
    loc = e.get("city") or e.get("location")
    if isinstance(loc, dict):
        return ", ".join(x for x in (loc.get("name"), (loc.get("country") or {}).get("name")) if x)
    return loc or e["name"]


def ethglobal():
    blob = _rsc(fetch("https://ethglobal.com/events"))
    i = blob.find('{"events":[')
    events = json.JSONDecoder().raw_decode(blob, i)[0]["events"] if i >= 0 else []
    out = []
    for e in events:
        if e.get("status") not in ("future", "upcoming", "live", "ongoing", None):
            continue
        if (e.get("type") or "").lower() != "hackathon":
            continue
        fmt = {"physical": "in-person", "virtual": "online", "online": "online", "hybrid": "hybrid"}.get(e.get("medium"), "unknown")
        out.append(record(
            "ethglobal", e["id"], e["name"], f"https://ethglobal.com/events/{e['slug']}",
            organizer="ETHGlobal", start=iso(e.get("startTime")), end=iso(e.get("endTime")),
            deadline=iso(e.get("signupDeadline")) or iso(e.get("startTime")), format=fmt,
            location="Online" if fmt == "online" else _eth_loc(e),
            country=((e.get("city") or {}).get("country") or {}).get("name") if isinstance(e.get("city"), dict) else None,
            description=e.get("tagline") or "", themes=["Web3", "Ethereum", "Crypto"],
            team_max=5, team_min=1,
        ))
    return out


# ---------------------------------------------------------------- Taikai
# The /hackathons page only server-renders the first 20 of ~280 challenges
# (sorted by its own "order", not by date), so an open one on page 12 was missed.
# The page's own public GraphQL API can ask for open challenges directly.
TAIKAI_GQL = "https://api.taikai.network/api/graphql"
TAIKAI_Q = """{ challengesPageInfo(where: $W) { recordCount pageCount }
  challenges(page: %d, orderBy: {order: desc}, where: $W) {
    id name slug isClosed prize participantsCount shortDescription allowedParticipants
    prizeCurrency { name } organization { name slug } industries { title } steps { startDate } } }"""
TAIKAI_WHERE = "{isClosed: {equals: false}, publishInfo: {state: {equals: ACTIVE}}}"


def taikai():
    out, page, pages = Batch(), 0, 1
    while page < pages and page < 10:
        d = fetch_json(TAIKAI_GQL, data={"query": (TAIKAI_Q % page).replace("$W", TAIKAI_WHERE)})
        if d.get("errors"):
            raise RuntimeError(f"taikai graphql: {d['errors'][0].get('message')}")
        info = d["data"]["challengesPageInfo"]
        pages, out.expected = info.get("pageCount") or 1, info.get("recordCount")
        for c in d["data"]["challenges"]:
            if c.get("isClosed"):
                continue
            dates = sorted(filter(None, (x.get("startDate") for x in c.get("steps") or [])))
            cur = (c.get("prizeCurrency") or {}).get("name") or "USD"
            prize = c.get("prize") or 0
            org = c.get("organization") or {}
            out.append(record(
                "taikai", c["id"], c["name"], f"https://taikai.network/{org.get('slug','')}/hackathons/{c['slug']}",
                organizer=org.get("name"), start=iso(dates[0]) if dates else None,
                end=iso(dates[-1]) if dates else None,
                deadline=iso(dates[-1]) if dates else None,
                format="online", location="Online (check page)",
                prize_usd=prize * FX.get(cur.upper(), 1) if prize else None,
                prize_text=f"{cur} {prize:,.0f}" if prize else None,
                description=c.get("shortDescription"),
                themes=[i.get("title", "") for i in c.get("industries") or []],
                participants=c.get("participantsCount"),
                flags={"invite_only": c.get("allowedParticipants") == "SELECTED"},
            ))
        page += 1
    return out


# ---------------------------------------------------------------- Hack Club
def hackclub():
    out = []
    for e in fetch_json("https://hackathons.hackclub.com/api/events/upcoming/"):
        fmt = "online" if e.get("virtual") else "hybrid" if e.get("hybrid") else "in-person"
        out.append(record(
            "hackclub", e["id"], e["name"], e.get("website"), organizer="Hack Club listing",
            start=iso(e.get("start")), end=iso(e.get("end")), deadline=iso(e.get("start")),
            format=fmt, location="Online" if fmt == "online" else ", ".join(
                x for x in (e.get("city"), e.get("country")) if x),
            country=e.get("country"),
            flags={"high_school": True, "note": "Hack Club lists high-school hackathons"},
        ))
    return out


# ---------------------------------------------------------------- Zindi
# Public API behind zindi.world (zindi.africa redirects there). Listing + one
# detail call per open challenge for its rules text (who may enter).
ZINDI = "https://api.zindi.world/v1/competitions"
ELIG = re.compile(r"eligib|citizen|resident|countr|nationals?\b|open to|individual|teams? of|students?|universit", re.I)


def _draft_text(v) -> str:
    """Zindi stores pages as draft.js blocks."""
    if isinstance(v, dict):
        c = v.get("content") if "content" in v else v
        if isinstance(c, dict) and "blocks" in c:
            return " ".join(b.get("text", "") for b in c["blocks"])
        return " ".join(_draft_text(x) for x in v.values() if isinstance(x, (dict, list)))
    if isinstance(v, list):
        return " ".join(_draft_text(x) for x in v)
    return v if isinstance(v, str) else ""


def zindi():
    d = fetch_json(f"{ZINDI}?page=0&per_page=100&status=open")
    out = Batch(expected=(d.get("meta") or {}).get("total_count"))
    for c in d.get("data") or []:
        if not c.get("open"):
            continue
        det = {}
        try:
            det = fetch_json(f"{ZINDI}/{c['id']}")
            det = det.get("data", det)
        except RuntimeError:
            pass  # listing fields are enough to show it
        rules = clean_text(_draft_text(det.get("rules")))
        elig = " ".join(x for x in re.split(r"(?<=[.!?])\s+", rules) if ELIG.search(x))[:2500]
        reward = re.sub(r"(?<=\d)[\s\u00a0](?=\d{3}\b)", "", c.get("reward") or "")
        reward = re.sub(r"^R(?=\d)", "ZAR ", reward)
        topics = [t for t in (det.get("topics") or []) + (det.get("skills") or []) if isinstance(t, str)]
        out.append(record(
            "zindi", c["id"], c["title"], f"https://zindi.africa/competitions/{c['id']}",
            organizer=(det.get("organization") or {}).get("name") if isinstance(det.get("organization"), dict)
            else c.get("organization") or "Zindi",
            start=iso(c.get("start_time")), end=iso(c.get("end_time")),
            deadline=iso(c.get("entries_close_at")) or iso(c.get("end_time")),
            format="online", location="Online", prize_usd=money_usd(reward), prize_text=c.get("reward"),
            themes=["data competition", "machine learning"] + ([c["kind"]] if c.get("kind") else []) + topics[:8],
            # Rules sentences feed the eligibility flags only, never theme matching
            # (rules boilerplate says "payment", "bank", "law" and would fake a fit).
            description=det.get("subtitle") or "", rules_text=elig, team_min=1,
            team_max=det.get("max_number_of_participants_per_team"),
            participants=c.get("participations_count"), image=c.get("image"),
            flags={"invite_only": bool(c.get("secret_code_required") or c.get("is_access_restricted")),
                   "data_competition": True,
                   "note": "Zindi marks this challenge access-restricted" if c.get("is_access_restricted") else None},
        ))
    return out


# ---------------------------------------------------------------- DrivenData
# Server-rendered list; each card carries its facts in data-* attributes.
def _ymd(v):
    v = re.sub(r"\D", "", v or "")[-8:]
    try:
        return iso(datetime.strptime(v, "%Y%m%d").replace(hour=23, minute=59))
    except ValueError:
        return None


def drivendata():
    s = fetch("https://www.drivendata.org/competitions/")
    i, j = s.find('id="active_competitions"'), s.find('id="completed_competitions"')
    block = s[i:j if j > i else len(s)] if i >= 0 else ""
    cards = re.split(r'(?=<div class="[^"]*panel-container active-comp)', block)[1:]
    out = Batch(expected=len(cards), note="expected = active cards on the page")
    for c in cards:
        attrs = dict(re.findall(r'data-([a-z_-]+)="([^"]*)"', c.split(">", 1)[0]))
        m = re.search(r'panel-competition-title">\s*<a href="(/(?:competitions|benchmarks)/(\d+)/[^"]+)"[^>]*>(.*?)</a>', c, re.S)
        if not m:
            continue
        desc = re.search(r'<p class="mt-2 mb-2 text-base color-body">(.*?)<span', c, re.S)
        host = re.search(r'alt="Hosted by ([^"]+)"', c)
        purse = float(attrs.get("total_prize_purse") or 0)
        practice = "reward_practice" in attrs
        cats = [k.split("_", 1)[1].replace("-", " ") for k in attrs if k.startswith(("category_", "tag_"))]
        out.append(record(
            "drivendata", m.group(2), clean_text(m.group(3)), "https://www.drivendata.org" + m.group(1),
            organizer=htmllib.unescape(host.group(1)) if host else "DrivenData",
            start=_ymd(attrs.get("start_date")), end=_ymd(attrs.get("deadline")),
            deadline=_ymd(attrs.get("deadline")), format="online", location="Online",
            prize_usd=purse or None, prize_text=f"USD {purse:,.0f}" if purse else ("Practice, no prize" if practice else None),
            themes=["data competition", "machine learning"] + (["practice"] if practice else []) + cats,
            description=desc.group(1) if desc else "", participants=int(attrs["joined"]) if attrs.get("joined", "").isdigit() else None,
            flags={"data_competition": True, "practice": practice},
        ))
    return out


# ---------------------------------------------------------------- AIcrowd
# Server-rendered cards. robots.txt asks for Crawl-delay: 10 (set in http.SLOW).
def aicrowd():
    out, seen = Batch(note="AIcrowd shows no total; running + starting soon"), set()
    for status in ("running", "starting_soon"):
        s = fetch(f"https://www.aicrowd.com/challenges?status={status}")
        i = s.find('id="challenges-div"')
        for c in re.split(r'(?=<div class="card card-challenge">)', s[i:] if i >= 0 else "")[1:]:
            m = re.search(r'<h5 class="card-title"><a href="(/challenges/([^"]+))">(.*?)</a>', c, re.S)
            if not m or m.group(2) in seen:
                continue
            seen.add(m.group(2))
            due = re.search(r'title="(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) UTC"', c)
            due = iso(due.group(1).replace(" ", "T") + "Z") if due else None
            if due and due > "2035":
                due = None  # "benchmark" challenges run to 2100: no real deadline
            phase = re.search(r'icon-time\.svg"\s*/>\s*([^<]+)', c)
            prize = re.search(r'class="prize-item[^"]*">\s*([^<]+)', c)
            text = re.search(r'<p class="card-text">(.*?)</p>', c, re.S)
            users = re.search(r'title="No\. of users">\s*<img[^>]*>\s*([\d.,k]+)', c)
            org = re.search(r'<div class="card-footer">.*?<span title="([^"]+)"', c, re.S)
            n = (users.group(1) if users else "").lower()
            ppl = int(float(n[:-1]) * 1000) if n.endswith("k") else int(n.replace(",", "")) if n.replace(",", "").isdigit() else None
            out.append(record(
                "aicrowd", m.group(2), clean_text(m.group(3)), "https://www.aicrowd.com" + m.group(1),
                organizer=htmllib.unescape(org.group(1)) if org else "AIcrowd",
                deadline=due, end=due, format="online", location="Online",
                prize_usd=money_usd(prize.group(1)) if prize else None,
                prize_text=clean_text(prize.group(1)) if prize else None,
                themes=["data competition", "machine learning"] +
                       [t.replace("_", " ") for t in re.findall(r"badge-outline'>#([^<]+)<", c)],
                description=f"{clean_text(text.group(1)) if text else ''} {clean_text(phase.group(1)) if phase else ''}",
                participants=ppl, flags={"data_competition": True, "starting_soon": status == "starting_soon"},
            ))
    return out


# ---------------------------------------------------------------- Hackster.io
# Contest list is server-rendered as a hypernova JSON blob ("ContestsPage").
def hackster():
    s = fetch("https://www.hackster.io/contests")
    m = re.search(r'data-hypernova-key="ContestsPage"[^>]*><!--(.*?)--></script>', s, re.S)
    d = json.loads(htmllib.unescape(m.group(1)))
    out = Batch()
    for c in (d.get("competition") or []) + (d.get("challenge") or []):
        if c.get("status") not in ("IN_PROGRESS", "PRE_REGISTRATION", "PRE_LAUNCH", None):
            continue
        pz = c.get("prize") or {}
        top = pz.get("cash_value")
        out.append(record(
            "hackster", c["id"], c["name"], "https://www.hackster.io" + c["url"],
            organizer=", ".join(x["name"] for x in c.get("sponsors") or []) or "Hackster.io",
            start=iso(c.get("start_date_in_pdt")), end=iso(c.get("end_date_in_pdt")),
            deadline=iso(c.get("end_date_in_pdt")), format="online", location="Online",
            prize_usd=top, prize_text=f"Top prize US${top:,.0f} ({pz.get('name','').strip()})" if top else None,
            themes=["Hardware"] + [x["name"] for x in c.get("sponsors") or []],
            description=c.get("teaser") or "", image=(c.get("cover_image") or {}).get("url"),
            flags={"signup_open": bool(c.get("is_registration_open"))},
        ))
    return out


# ---------------------------------------------------------------- Kaggle
# Kaggle's own gRPC-web JSON endpoint (the /competitions page calls it). Reachable
# without an account. Its list is every competition ever, so only ones that are
# still open (deadline in the future) or have no deadline are kept.
KAGGLE = "https://www.kaggle.com/api/i/competitions.CompetitionService/ListCompetitions"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def kaggle():
    out, seen, token, pages = Batch(), set(), "", 0
    while pages < 12:
        body = {"pageSize": 100}
        if token:
            body["pageToken"] = token
        d = fetch_json(KAGGLE, data=body)
        cs = d.get("competitions") or []
        if not cs:
            break
        for c in cs:
            cid = c.get("id")
            if cid in seen:
                continue
            seen.add(cid)
            due = iso(c.get("deadline"))
            if due and (due < _utcnow() or due > "2035"):
                continue  # ended, or an endlessly-running benchmark
            rew = c.get("reward") or {}
            qty = rew.get("quantity")
            cur = (rew.get("id") or "USD").upper()
            usd = qty * FX.get(cur, 1) if qty else None
            cats = [x.get("displayName") for x in c.get("categories") or [] if x.get("displayName")]
            out.append(record(
                "kaggle", cid, c.get("title"),
                f"https://www.kaggle.com/competitions/{c.get('competitionName')}",
                organizer=(c.get("organization") or {}).get("name") or c.get("hostName") or "Kaggle",
                start=iso(c.get("dateEnabled")), end=due, deadline=due,
                format="online", location="Online",
                prize_usd=usd, prize_text=f"{cur} {qty:,.0f}" if qty else None,
                themes=["data competition", "machine learning"] + cats,
                description=c.get("briefDescription") or "",
                team_max=c.get("maxTeamSize"),
                participants=c.get("totalTeams") or c.get("totalCompetitors"),
                flags={"data_competition": True},
            ))
        token = d.get("nextPageToken")
        if not token:
            break
        pages += 1
    out.expected = len(out)
    out.note = "expected = open competitions in Kaggle's list"
    return out


# ---------------------------------------------------------------- Luma
# Luma's discover feed (the luma.com/discover page calls it). No key needed. There
# is no cross-calendar hackathon listing, but this search answers "hackathon".
LUMA = "https://api.lu.ma/discover/get-paginated-events"


def luma():
    out, seen = Batch(note="Luma discover search 'hackathon'; the site gives no total"), set()
    cursor = ""
    for _ in range(6):
        url = f"{LUMA}?pagination_limit=50&query=hackathon"
        if cursor:
            url += f"&pagination_cursor={cursor}"
        d = fetch_json(url)
        for e in d.get("entries") or []:
            ev = e.get("event") or {}
            aid = ev.get("api_id")
            if not aid or aid in seen:
                continue
            seen.add(aid)
            geo = ev.get("geo_address_info") or {}
            loc = ", ".join(x for x in (geo.get("city"), geo.get("region"), geo.get("country")) if x) \
                or geo.get("address")
            lt = (ev.get("location_type") or "").lower()
            addr = (geo.get("address") or "").lower()
            fmt = "online" if lt == "online" or addr == "online" else \
                "in-person" if lt == "offline" else "unknown"
            out.append(record(
                "luma", aid, ev.get("name"), f"https://lu.ma/{ev.get('url')}",
                organizer=(e.get("calendar") or {}).get("name"),
                start=iso(ev.get("start_at")), end=iso(ev.get("end_at")), deadline=iso(ev.get("end_at")),
                format=fmt, location=loc, country=geo.get("country"),
                themes=["hackathon"], description=(e.get("calendar") or {}).get("description_short") or "",
                participants=e.get("guest_count"), image=ev.get("cover_url"),
            ))
        cursor = d.get("next_cursor") or ""
        if not d.get("has_more") or not cursor:
            break
    return out


# ---------------------------------------------------------------- HackerEarth
# Its own JSON feed behind the Compete page. The site's filters are client-side, so
# everything comes back and the adapter keeps open hackathons. This host answers a
# datacenter/VPN IP with a 403 "ANONYMOUS IP" wall; from a normal connection it serves
# our honest user agent (see RADAR_SOURCE_IP in http.py).
HACKEREARTH = "https://www.hackerearth.com/api/community/challenges/compete/"


def hackerearth():
    d = fetch_json(HACKEREARTH)
    rows = d.get("data") or []
    out, now = Batch(note="expected = open hackathons in the Compete feed"), _utcnow()
    for c in rows:
        if (c.get("type") or "").lower() != "hackathon":
            continue
        end = iso(c.get("end"))
        if end and end < now:
            continue  # already closed
        url = c.get("url") or ""
        if url and not url.startswith("http"):
            url = "https://www.hackerearth.com" + url
        out.append(record(
            "hackerearth", c.get("slug") or c.get("title"), c.get("title"), url,
            organizer=c.get("company_name"), start=iso(c.get("start")), end=end, deadline=end,
            format="online", location="Online", country="India",
            themes=["hackathon"], participants=c.get("subscription_count"),
            team_min=c.get("min_team_size"), team_max=c.get("max_team_size"),
            flags={"india_platform": True},
        ))
    out.expected = len(out)
    return out


# ---------------------------------------------------------------- Eventbrite
# Two independent ways in, so one breaking does not take the source down:
#   1. the directory's own search API. It rejects a plain call with 401 because it wants a
#      CSRF cookie from the listing page — not the user token the documented OAuth API needs.
#   2. the listing page itself, which ships its own results inside `window.__SERVER_DATA__`.
# No user token either way, and both are filtered the same.
EVB_LISTS = ("https://www.eventbrite.com/d/online/hackathon/",
             "https://www.eventbrite.com/d/hackathon/")
EVB_SEARCH = "https://www.eventbrite.com/api/v3/destination/search/"
EVB_PAGES = 4  # 50 per page; the site reports ~19 pages, we read the first few
# Eventbrite's search is fuzzy: "hackathon" also returns workshops, talks and meetups
# (a run pulled in "Mental Health and Homelessness"). Keep only events that read as one.
HACK_RX = re.compile(r"hack|datathon|buildathon|codefest|hackfest|ideathon", re.I)


def _evb_when(date_s, time_s, tzname):
    """Eventbrite gives a local date, a local time and an IANA zone separately."""
    if not date_s:
        return None
    clock = (time_s or "00:00")[:5]
    try:
        from zoneinfo import ZoneInfo
        return iso(datetime.fromisoformat(f"{date_s}T{clock}:00").replace(tzinfo=ZoneInfo(tzname)))
    except Exception:
        return iso(f"{date_s}T{clock}:00")  # no tz data: keep it, slightly off


def _evb_row(r):
    """One result. The API and the page's own data use the same field names."""
    eid = r.get("id") or r.get("eventbrite_event_id")
    if not eid:
        return None
    online = bool(r.get("is_online_event"))
    city = country = None
    locs = r.get("locations") or []
    if locs and isinstance(locs[0], dict):
        a = locs[0].get("address") or {}
        if isinstance(a, dict):
            city = ", ".join(x for x in (a.get("city"), a.get("region"), a.get("country")) if x)
            country = a.get("country")
    end = _evb_when(r.get("end_date"), r.get("end_time"), r.get("timezone"))
    return record(
        "eventbrite", eid, r.get("name"), r.get("url"),
        start=_evb_when(r.get("start_date"), r.get("start_time"), r.get("timezone")),
        end=end, deadline=end,
        format="online" if online else "in-person",
        location="Online" if online else (city or None), country=country,
        themes=[t.get("display_name") for t in r.get("tags") or [] if t.get("display_name")],
        description=r.get("summary") or r.get("full_description") or "",
        image=(r.get("image") or {}).get("url"),
        flags={"is_cancelled": True} if r.get("is_cancelled") else {},
    )


def _evb_add(out, seen, r):
    if not HACK_RX.search(f"{r.get('name') or ''} {r.get('summary') or ''}"):
        return  # not actually a hackathon
    row = _evb_row(r)
    if row and row["id"] not in seen:
        seen.add(row["id"])
        out.append(row)


def _evb_embedded(html):
    """Fallback: the listing page carries its own search results in __SERVER_DATA__."""
    m = re.search(r"window\.__SERVER_DATA__\s*=\s*(\{.*?\})\s*(?:;\s*window\.|</script>)", html, re.S)
    if not m:
        return []
    try:
        d = json.loads(m.group(1))
    except ValueError:
        return []
    return ((d.get("search_data") or {}).get("events") or {}).get("results") or []


def _evb_via_api(out, seen, tok, list_url):
    headers = {"Cookie": f"csrftoken={tok}", "X-CSRFToken": tok,
               "Referer": list_url, "Origin": "https://www.eventbrite.com"}
    raw = 0
    for page in range(1, EVB_PAGES + 1):
        d = fetch_json(EVB_SEARCH, headers=headers, data={
            "event_search": {"q": "hackathon", "dates": "current_future",
                             "page": page, "page_size": 50},
            "expand.destination_event": ["primary_venue", "ticket_availability", "tags"],
        })
        results = (d.get("events") or {}).get("results") or []
        if not results:
            break
        raw += len(results)
        for r in results:
            _evb_add(out, seen, r)
    return raw


def eventbrite():
    out, seen = Batch(note="search is fuzzy, so results are filtered to hackathon-named events"), set()
    html, tok, list_url = "", "", EVB_LISTS[0]
    for url in EVB_LISTS:
        try:
            html, hdrs = fetch_headers(url)
        except Exception:
            continue  # try the next listing URL
        list_url = url
        m = re.search(r"csrftoken=([^;,\s]+)", hdrs.get("set-cookie", ""))
        if m:
            tok = m.group(1)
            break
    raw = 0
    if tok:
        try:
            raw += _evb_via_api(out, seen, tok, list_url)
        except Exception:
            out.clear()  # token went stale or the API moved: use the page we already hold
    if not out and html:
        embedded = _evb_embedded(html)
        if embedded:
            out.note += "; API unavailable, read the listing page's own data"
        raw += len(embedded)
        for r in embedded:
            _evb_add(out, seen, r)
    if not html:
        raise RuntimeError("Eventbrite: could not load any listing page")
    if raw == 0:
        # Nothing parsed from either route: report it rather than look like a quiet zero.
        raise RuntimeError("Eventbrite: neither the API nor the page yielded results (layout change?)")
    return out


SOURCES = {
    "devpost": devpost, "devfolio": devfolio, "unstop": unstop, "mlh": mlh,
    "lablab": lablab, "ethglobal": ethglobal, "taikai": taikai, "hackclub": hackclub,
    "zindi": zindi, "drivendata": drivendata, "aicrowd": aicrowd,
    "hackster": hackster, "kaggle": kaggle, "luma": luma, "hackerearth": hackerearth,
    "eventbrite": eventbrite,
}

# Shown on the Coverage page next to each source's numbers.
SOURCE_NOTES = {
    "devpost": "Public API caps per_page at 40; paged to total_count.",
    "devfolio": "Search API, open + upcoming.",
    "unstop": "Public API, open hackathons (India platform). Its total counts entries it never serves: pages come back short (42, 47, 49 of 50), so partial is the site, not the adapter.",
    "mlh": "Season page props; student events. Site gives no total.",
    "lablab": "Page embeds every event ever (~140); only those not yet ended are kept. Site gives no total.",
    "ethglobal": "Page embeds all ~340 events (meetups, summits, past); only future hackathons are kept.",
    "taikai": "Public GraphQL API, open challenges (the page itself only renders the first 20).",
    "hackclub": "Public API, upcoming high-school events. No total.",
    "zindi": "Public API, open challenges; rules read for eligibility. Many are Africa-only.",
    "drivendata": "Server-rendered list; includes long-running practice competitions.",
    "aicrowd": "Server-rendered list, running + starting soon. Crawl-delay 10 s respected.",
    "hackster": "Hypernova page data; hardware contests. Site gives no total. Contest pages return 403, so rules are not read.",
    "kaggle": "Own gRPC-web JSON endpoint, no account needed. Only competitions still open are kept.",
    "luma": "Discover search for 'hackathon', no key needed. Up to ~50 results; the site gives no total.",
    "hackerearth": "Own Compete feed. Filters are client-side, so all challenges are fetched and open hackathons kept. This host blocks datacenter/VPN IPs as 'ANONYMOUS IP'; set RADAR_SOURCE_IP to your own connection if your traffic leaves through a VPN.",
    "eventbrite": "Directory search API with a CSRF cookie taken from the listing page (no user token). The search is fuzzy, so results are filtered to hackathon-named events; only the first pages are read.",
}

# Places we know list hackathons but do not collect from, and why.
NOT_COVERED = [
    {"source": "sponsor-run challenges", "reason": "Google, AWS, Microsoft, Meta etc. run their own pages; no shared feed. Check by hand."},
]
