#!/usr/bin/env python3
"""Write the scored board out as CSV/JSON under export/.

  python3 export.py     # export/hackathons.csv, export/actionable.csv, export/hackathons.json

Reads the same database the UI reads, so run a scrape first if it looks empty.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from radar import store

OUT = Path(__file__).resolve().parent / "export"
FULL_COLS = ["score", "tier", "title", "source", "format", "location", "deadline", "prize_usd",
             "why", "blockers", "warnings", "url", "page_read", "is_new", "mark", "summary"]
SHORT_COLS = ["score", "tier", "title", "source", "format", "location", "deadline", "prize_usd",
              "url", "why"]


def _why(h):
    return " | ".join(r["why"] for r in h.get("reasons") or [])


def _write(name, cols, rows):
    with (OUT / name).open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)


def main():
    OUT.mkdir(exist_ok=True)
    hs = store.all_scored()
    if not hs:
        print("nothing to export yet — run: python3 radar.py scrape")
        return
    (OUT / "hackathons.json").write_text(json.dumps(hs, indent=2, ensure_ascii=False))
    _write("hackathons.csv", FULL_COLS, [
        [h.get("score"), h.get("tier"), h.get("title"), h.get("source"), h.get("format"),
         h.get("location"), h.get("deadline"), h.get("prize_usd"), _why(h),
         ",".join(h.get("blockers") or []), ",".join(h.get("warnings") or []),
         h.get("url"), h.get("page_read"), h.get("is_new"), h.get("mark"),
         (h.get("summary") or "").replace("\n", " ")[:300]]
        for h in hs])
    act = [h for h in hs if h["tier"] in ("go", "maybe")]
    _write("actionable.csv", SHORT_COLS, [
        [h["score"], h["tier"], h["title"], h["source"], h["format"], h.get("location"),
         h.get("deadline"), h.get("prize_usd"), h["url"], _why(h)]
        for h in act])
    print(f"{len(hs)} events -> {OUT}/ ({len(act)} go+maybe)")


if __name__ == "__main__":
    main()
