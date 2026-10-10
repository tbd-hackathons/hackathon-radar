"""Fit scoring: how well one hackathon suits the team in profile.json.

Score 0-100 from six parts, plus hard blockers:
  reach  30  can we actually take part (online, or a trip we would make)
  theme  22  overlap with what we already build (profile "themes", weights)
  edge    8  depth: the deepest of Fury's edge areas the event hits
             (payments 8 > fintech / banking & fraud 6 > privacy & compliance 5
             > gov/legal 4 > B2B commerce 3 > data/BI 2 > AI agents / dev tools 1)
  prize  15  log scale, $1k = 5, $10k = 10, $100k+ = 15
  timing 15  enough days left to ship, not so far out it is noise
  team   10  a team of one human + AI agents is allowed
Every point added or taken away is written into `reasons`, so the UI can
show why a hackathon ranks where it does.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone

APAC = {"Japan", "South Korea", "Korea", "Taiwan", "Hong Kong", "Australia", "New Zealand",
        "India", "China", "Singapore", "Malaysia", "Thailand", "Vietnam", "Philippines",
        "Indonesia", "Bangladesh", "Pakistan", "Sri Lanka", "Nepal", "Cambodia"}


def _kw_regex(kw: str):
    kw = kw.strip().lower()
    if len(kw) <= 3:
        return re.compile(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])")
    return re.compile(r"(?<![a-z0-9])" + re.escape(kw))


def _days_until(ts):
    if not ts:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return (dt - datetime.now(timezone.utc)).total_seconds() / 86400


def _country_of(h):
    if h.get("country"):
        return h["country"]
    loc = h.get("location") or ""
    for c in APAC | {"United States", "USA", "Canada", "United Kingdom", "Germany", "France",
                     "Spain", "Portugal", "Netherlands", "Brazil", "Nigeria", "Kenya", "UAE",
                     "United Arab Emirates", "Italy", "Poland", "Switzerland", "Mexico"}:
        if c.lower() in loc.lower():
            return c
    if re.search(r",\s*[A-Z]{2}$|New York|California|San Francisco|Texas|Boston", loc):
        return "United States"
    return None


def score(h: dict, profile: dict, enrich: dict | None = None) -> dict:
    enrich = enrich or {}
    reasons, blockers, warnings = [], [], []
    add = lambda part, pts, why: reasons.append({"part": part, "pts": round(pts, 1), "why": why})

    # ---- reach
    fmt = h.get("format")
    country = _country_of(h)
    home = profile["home_country"]
    if fmt == "online":
        reach = 30; add("reach", 30, "Online: join from Jakarta")
    elif fmt == "hybrid":
        reach = 24; add("reach", 24, "Hybrid: online track available")
    elif country == home:
        reach = 28; add("reach", 28, f"In person in {home}")
    elif country in profile["travel_ok_countries"]:
        reach = 18; add("reach", 18, f"In person in {country}: short regional trip")
    elif country in profile["travel_maybe_countries"]:
        reach = 8; add("reach", 8, f"In person in {country}: long trip")
    else:
        reach = 2; add("reach", 2, f"In person{' in ' + country if country else ''}: travel needed")

    # ---- theme
    title_tags = " ".join([h.get("title") or ""] + list(h.get("themes") or [])).lower()
    # Listing text only: rules pages are legal boilerplate ("law", "government") and would
    # make every event look like RegTech.
    desc = (h.get("description") or "").lower()
    theme_max, edge_max = profile.get("theme_max", 22), profile.get("edge_max", 8)
    theme, matched, edge_hits = 0.0, [], []
    for g in profile["themes"]:
        rx = [_kw_regex(k) for k in g["keywords"]]
        if any(r.search(title_tags) for r in rx):
            share, label = 1.0, g["name"]
        elif any(r.search(desc) for r in rx):
            share, label = 0.6, g["name"] + " (in text)"
        else:
            continue
        cf = g.get("conflict")
        if cf and any(_kw_regex(k).search(title_tags + " " + desc) for k in cf["with"]):
            # e.g. an Indonesian investing product: not a fit while it may clash with the day job
            warnings.append(cf.get("warning", "conflict"))
            add("edge", 0, f"{g['name']}, but it targets {home}: check conflict of interest first")
            continue
        theme += g["weight"] * share; matched.append(label)
        if g.get("edge"):
            edge_hits.append((g["edge"] * share, label))
    if matched:
        add("theme", min(theme, theme_max), "Fits: " + ", ".join(matched))
    else:
        add("theme", 0, "No overlap with our product areas")
    title_only = (h.get("title") or "").lower()
    weak = [k for k in profile.get("weak_fit_keywords", []) if _kw_regex(k).search(title_tags)]
    weak_in_title = [k for k in weak if _kw_regex(k).search(title_only)]
    if weak and not weak_in_title and theme < 10:
        theme -= 8; add("theme", -8, "Weak fit: " + ", ".join(weak))
    dr = profile.get("downrank")
    if dr:
        hits = [k for k in dr["keywords"] if _kw_regex(k).search(title_tags)]
        if hits:
            theme += dr["pts"]; add("theme", dr["pts"], f"{dr['why']} ({', '.join(hits[:3])})")
            edge_hits = []  # crypto trading is not Fury's payments/fintech edge
    theme = max(min(theme, theme_max), 0)
    if weak_in_title:
        # The event is about this subject: generic matches elsewhere do not make it our lane.
        # Applied after the cap, so a long list of incidental matches cannot absorb it.
        theme = max(theme - 12, 0); add("theme", -12, "Weak fit, the event's subject: " + ", ".join(weak_in_title))

    # ---- edge: the deepest edge area hit, not the sum (breadth is already in theme)
    if edge_hits:
        edge, label = max(edge_hits)
        edge = min(edge, edge_max)
        add("edge", edge, f"Fury's edge: {label}")
    else:
        edge = 0
        if not any(r["part"] == "edge" for r in reasons):
            add("edge", 0, "Outside Fury's edge areas")

    # ---- prize
    usd = h.get("prize_usd") or enrich.get("prize_usd")
    if usd:
        prize = max(0.0, min(15.0, 5 * math.log10(max(usd, 1)) - 10))
        add("prize", prize, f"Prize pool ≈ US${usd:,.0f}")
    else:
        prize = 3; add("prize", 3, "Prize not stated in listing")

    # ---- timing
    days = _days_until(h.get("deadline") or h.get("end"))
    if days is None:
        timing = 6; add("timing", 6, "Deadline not stated")
    elif days < 0:
        timing = 0; blockers.append("too_late"); add("timing", 0, "Deadline passed")
    elif days < profile.get("min_days_to_build", 2):
        timing = 0; blockers.append("too_late"); add("timing", 0, f"Only {days:.1f} days left")
    elif days < 7:
        timing = 9; add("timing", 9, f"{days:.0f} days left: sprint")
    elif days <= 45:
        timing = 15; add("timing", 15, f"{days:.0f} days left: good window")
    elif days <= 120:
        timing = 11; add("timing", 11, f"{days:.0f} days out: plan ahead")
    else:
        timing = 6; add("timing", 6, f"{days:.0f} days out: far")

    # ---- team
    tmin = enrich.get("team_min") or h.get("team_min")
    if enrich.get("solo_allowed") or (tmin is not None and tmin <= 1):
        team = 10; add("team", 10, "Solo entry allowed")
    elif tmin is None:
        team = 6; add("team", 6, "Team size not stated")
    else:
        team = 1; warnings.append("team_min_over_1"); add("team", 1, f"Needs at least {tmin} people")

    # ---- eligibility flags (listing + page read)
    fl = dict(h.get("flags") or {})
    fl.update({k: v for k, v in enrich.items() if isinstance(v, bool)})
    if profile.get("is_student") is False and any(_kw_regex(k).search(title_tags) for k in profile.get("student_keywords", [])):
        fl["students_only"] = True
    if profile.get("is_student") is False and fl.get("students_only"):
        blockers.append("students_only")
    if fl.get("high_school"):
        blockers.append("high_school")
    if fl.get("invite_only"):
        blockers.append("invite_only")
    if fl.get("country_excludes_home"):
        blockers.append("country_excludes_home")
    if fl.get("ai_banned"):
        blockers.append("ai_banned")
    if fl.get("women_only"):
        warnings.append("women_only")
    if fl.get("paid_entry"):
        warnings.append("paid_entry")
    if fl.get("india_platform") and home != "India":
        warnings.append("india_platform"); reach = max(0, reach - 6)
        add("reach", -6, "Hosted on an India platform: check that non-Indian winners get paid")
    if fl.get("ai_allowed"):
        add("team", 0, "Rules allow AI tools")
    if fl.get("home_mentioned"):
        theme = min(theme_max, theme + 4); add("theme", 4, f"Page mentions {home}")

    blockers = [b for b in dict.fromkeys(blockers) if b in profile["blockers"]]
    warnings = [w for w in dict.fromkeys(warnings) if w in profile.get("warnings", [])]
    total = round(reach + theme + edge + prize + timing + team)
    if blockers:
        tier = "blocked"
    elif total >= 70:
        tier = "go"
    elif total >= 52:
        tier = "maybe"
    else:
        tier = "skip"
    return {"score": total, "tier": tier, "parts": {"reach": reach, "theme": round(theme, 1),
            "edge": round(edge, 1), "prize": round(prize, 1), "timing": timing, "team": team},
            # The effective pool, which may have come from the page rather than the listing.
            # Without this the score says "Prize pool ≈ US$100,000" while the field reads None.
            "prize_usd": usd,
            "reasons": reasons, "blockers": blockers, "warnings": warnings, "days_left": days}
