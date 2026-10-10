"""Second pass: read the hackathon's own page (and Devpost rules page) for the
facts listings leave out: who may enter, whether AI tools are allowed, which
countries are excluded, solo entry, real prize pool.

Every flag keeps the sentence it came from in `evidence`, so a human can check
the call in one glance instead of trusting a regex.
"""
from __future__ import annotations

import re

from .http import fetch
from .sources import clean_text, money_usd

S = r"[^.\n]{0,80}"  # stay inside one sentence

RULES = {
    "students_only": re.compile(
        r"(only|exclusively)\s+(open\s+)?(to\s+)?(currently\s+)?(enrolled\s+)?(university|college|undergrad\w*|graduate|high[- ]school)?\s*students"
        r"|must be (a |an )?(currently )?(enrolled|registered|full[- ]time) (student|in )"
        r"|open to (all )?(university |college |undergraduate |engineering )students(?! and)"
        r"|open to (current |enrolled )?students\b(?! and)", re.I),
    # "only" is the unambiguous form, but pages also write "Open to high school students
    # worldwide (ages ~13-19)", which none of the narrow patterns caught — so EurekaDev ranked
    # as a Go match for a non-student adult. Cover the open-to phrasing and the age range.
    "high_school": re.compile(
        r"high[- ]school(ers)?\s+only|for high[- ]school students"
        r"|open to[^.\n]{0,40}high[- ]school|eligib\w*[^.\n]{0,60}high[- ]school"
        r"|ages?\s*~?\s*1[2-5]\s*(-|–|to)\s*1[7-9]", re.I),
    "invite_only": re.compile(r"open only to members of|must be a member of the [^.]{0,40}program", re.I),
    "women_only": re.compile(r"(women|female)[- ]only|only (open to )?(women|females)", re.I),
    "ai_banned": re.compile(
        r"(not (be )?(allowed|permitted)|prohibited|banned|disallowed|forbidden)" + S +
        r"(ai[- ]generated|generative ai|chatgpt|copilot|llms?\b|ai tools|ai[- ]assisted)"
        r"|(ai[- ]generated code|use of (generative )?ai( tools)?|chatgpt|ai tools)" + S +
        r"(is |are )?(strictly )?(not allowed|not permitted|prohibited|banned|forbidden)", re.I),
    "ai_allowed": re.compile(
        r"(ai tools|ai coding|copilot|cursor|claude|chatgpt|ai assistants?|generative ai|vibe[- ]cod\w*)" + S +
        r"(are |is )?(allowed|permitted|encouraged|welcome)", re.I),
    "solo_allowed": re.compile(
        r"teams? of (1|one)\b|\b1\s*(-|–|to)\s*\d+\s*(team )?(members|people|participants|hackers)"
        r"|individuals? (or|and) teams|solo (participants|hackers|entries|builders)|individually or|participate (alone|solo|individually)"
        r"|(team|participate) (as an? )?individual", re.I),
}
NOT_STUDENT_ONLY = re.compile(r"professionals|anyone|everyone|open to all|non-students|all developers|working developers|startups", re.I)
EXCLUDE_NEAR = re.compile(r"exclud\w*|not eligible|ineligible|except|void|prohibited|embargo|sanction", re.I)
RESIDENTS = re.compile(r"(open (only )?to|limited to|must be|restricted to)\s+(legal\s+)?(residents?|citizens?)\s+of\s+(?P<list>[^.]{0,250})", re.I)
WORLDWIDE = re.compile(r"worldwide|any country|all countries|globally|around the world|international", re.I)
NATIONAL_ONLY = re.compile(
    r"\b(across|throughout|all over|from all over|anywhere in) india\b|\bnational[- ]level\b|\bnationwide\b|\bpan[- ]india\b"
    r"|\b(indian|india[- ]based) (students|citizens|nationals|residents|colleges|institutions|teams|startups)\b"
    r"|\b(students|colleges|institutions|participants|teams|startups) (from|across|in|of) india\b", re.I)
TEAM_MIN = re.compile(r"teams? (of|between) (\d)\s*(-|–|to|and)\s*\d", re.I)


def _snip(text, m, pad=90):
    a, b = max(0, m.start() - pad), min(len(text), m.end() + pad)
    return ("…" if a else "") + text[a:b].strip() + ("…" if b < len(text) else "")


def analyse(text: str, home: str = "Indonesia") -> dict:
    out = {"evidence": []}
    for flag, rx in RULES.items():
        m = rx.search(text)
        if not m:
            continue
        if flag == "students_only" and NOT_STUDENT_ONLY.search(text[max(0, m.start() - 300): m.end() + 300]):
            continue
        out[flag] = True
        out["evidence"].append({"flag": flag, "text": _snip(text, m)})
    if out.get("ai_banned") and out.get("ai_allowed"):
        out.pop("ai_banned")  # mixed signals: keep the permissive reading, evidence stays

    low = text.lower()
    h = home.lower()
    for m in re.finditer(re.escape(h), low):
        window = text[max(0, m.start() - 160): m.end() + 160]
        if EXCLUDE_NEAR.search(window):
            out["country_excludes_home"] = True
            out["evidence"].append({"flag": "country_excludes_home", "text": "…" + window.strip() + "…"})
            break
    else:
        m = RESIDENTS.search(text)
        if m and h not in m.group("list").lower() and not WORLDWIDE.search(m.group("list")):
            out["country_excludes_home"] = True
            out["evidence"].append({"flag": "country_excludes_home", "text": _snip(text, m, 40)})
        elif h in low:
            out["home_mentioned"] = True
            i = low.find(h)
            out["evidence"].append({"flag": "home_mentioned", "text": "…" + text[max(0, i - 100): i + 100].strip() + "…"})

    if not out.get("country_excludes_home") and h != "india":
        m = NATIONAL_ONLY.search(text)
        if m and h not in text[max(0, m.start() - 200): m.end() + 200].lower():
            out["country_excludes_home"] = True
            out["evidence"].append({"flag": "country_excludes_home", "text": _snip(text, m)})

    m = TEAM_MIN.search(text)
    if m:
        out["team_min"] = int(m.group(2))
    prize_bits = " ".join(text[max(0, x.start() - 150): x.end() + 150] for x in re.finditer(r"prize|reward|bount", text, re.I))
    usd = money_usd(prize_bits)
    if usd:
        out["prize_usd"] = usd
    return out


def enrich_one(h: dict, home: str) -> dict:
    # Devpost: the rules page carries eligibility, countries and prizes; one hit per event.
    urls = [h["url"].rstrip("/") + "/rules"] if h["source"] == "devpost" else [h["url"]]
    text = ""
    for u in urls:
        try:
            text += " " + clean_text(fetch(u, timeout=25, retries=1))
        except Exception as e:
            text += ""
            if u == urls[0]:
                return {"error": str(e)[:200], "evidence": []}
    if len(text.strip()) < 500:
        # Empty or near-empty body = bot check or JS-only page. Not a read; retry next run.
        return {"error": "page not readable (bot check or JS-only page)", "evidence": []}
    text = text[:300_000]
    res = analyse(text, home)
    res["text_sample"] = text[:2500]
    res["chars"] = len(text)
    return res


def merge(listing: dict, page: dict) -> dict:
    """Combine flags found in the listing text with flags found on the page."""
    out = dict(page or {})
    for k, v in (listing or {}).items():
        if isinstance(v, bool):
            out[k] = out.get(k) or v
        elif k == "evidence":
            seen = {(e["flag"], e["text"][:60]) for e in out.get("evidence", [])}
            out["evidence"] = out.get("evidence", []) + [e for e in v if (e["flag"], e["text"][:60]) not in seen]
        elif k not in out:
            out[k] = v
    if out.get("ai_banned") and out.get("ai_allowed"):
        out.pop("ai_banned")
    return out
