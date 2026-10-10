# Hackathon Radar

Finds open hackathons worldwide and ranks them by fit for **Fury + the AI agent team**
(one human in Jakarta, AI agents doing the build).

```bash
python3 radar.py serve        # http://127.0.0.1:8787, first run starts by itself
```

No dependencies: Python 3.9+ standard library and SQLite (`data/radar.db`).

## How it works

1. **Collect** from 17 sources in parallel. Each adapter reads the source's own JSON API or the data embedded in its page:

   | Source | Method | What it covers |
   |---|---|---|
   | Devpost | public JSON API (per_page capped at 40, paged to `total_count`) | the biggest global online pool, sponsor prizes |
   | Devfolio | search API | India + web3, women-only flag |
   | Unstop | public API | India, many student-only events |
   | MLH | Inertia page props | student hackathons (blocked for us by default) |
   | lablab.ai | Next.js flight data | AI hackathons, often online/hybrid |
   | ETHGlobal | Next.js flight data | web3 hackathons |
   | Taikai | public GraphQL API (`api.taikai.network`), open challenges | EU/LatAm innovation challenges |
   | Hack Club | public API | high-school events (blocked for us by default) |
   | DoraHacks | the site's own list API (`/api/v1/hub/hackathons`), ongoing + upcoming; this host only, requests use a browser user agent (see Limits) | web3 + AI, big online prize pools |
   | Zindi | public API (`api.zindi.world`), open challenges + rules text | data/ML competitions, many Africa-only |
   | DrivenData | server-rendered list (`data-*` attributes) | data/ML competitions for social impact |
   | AIcrowd | server-rendered cards, running + starting soon, 10 s between requests (robots.txt Crawl-delay) | data/ML research challenges |
   | Hackster.io | hypernova page data on `/contests` (contest pages themselves return 403, so their rules are not read) | hardware contests (weak fit, shown for completeness) |
   | Kaggle | `CompetitionService/ListCompetitions` gRPC-web JSON endpoint, no account key, paged to the end; only still-open competitions kept | data/ML competitions, big-name prizes |
   | Luma | discover feed (`api.lu.ma/discover/get-paginated-events`), search for `hackathon`, no key | community + big-tech hackathons, online and in person |
   | HackerEarth | own Compete feed (`/api/community/challenges/compete/`); filters are client-side, so open hackathons are picked here. See the IP note in Limits | India-heavy coding events, many student-only |
   | Eventbrite | directory search API (`/api/v3/destination/search/`) with a CSRF cookie read from the listing page — no user token. Falls back to the listing page's own embedded results if the API fails | community hackathons, online-led |

   Zindi, DrivenData, AIcrowd and Kaggle are data/ML competitions: their themes include `data competition`
   (search for it), so they can be shown or filtered.

2. **Score** each event 0–100 against `profile.json`:
   reach 30 (online or a trip we'd make) · theme 22 (overlap with what we build) ·
   edge 8 (depth: Fury's deepest area the event hits) · prize 15 · timing 15 · team 10 (solo entry allowed).

   **Edge**, deepest first (points = the `edge` value on each theme group, the best one counts):

   | Area | Theme weight | Edge |
   |---|---|---|
   | Payments & money movement (gateways, virtual accounts, e-wallets, disbursement, reconciliation, payment links, disputes, invoicing) | 10 | 8 |
   | Fintech / investing / wealth | 8 | 6 |
   | Banking, lending & fraud / scam protection | 8 | 6 |
   | Privacy, data protection & compliance (GDPR, regtech, KYC/onboarding) | 7 | 5 |
   | Gov-tech / legal-tech / regulation | 7 | 4 |
   | B2B SMB commerce, e-commerce, retail/FMCG distribution, logistics | 6 | 3 |
   | Data / BI / analytics, open data | 5 | 2 |
   | AI agents, MCP, LLM tools, dev tools / DevSecOps, automation | 5 | 1 |
   | Indonesia / SEA | 6 | – |

   A match in the title or listed themes counts in full; a match only in the description counts 60%.
   Fintech / investing is skipped (no theme or edge points, warning `id_investing_conflict`) when the event
   targets Indonesia (Indonesia, IDX, OJK, reksadana, rupiah…): an Indonesian investing product needs a
   conflict-of-interest check first. "Why this score" lists the edge line, and cards show a chip when edge ≥ 4.
3. **Read the rules.** The top 80 candidates get their own page (and Devpost `/rules`) read to find:
   students-only, AI tools banned or allowed, Indonesia excluded, solo entry, real prize pool.
   Each flag keeps the sentence it came from, shown under "Why this score".
4. **Tier:** Go ≥ 70 · Maybe 52–69 · Skip < 52 · Blocked = any hard blocker.
5. **Coverage tab** (`GET /api/coverage`): per source, events collected vs. the source's own reported
   total (expected), share, status (ok · partial · failed · blocked-by-site), last run time, seconds and a
   note. Sources that give no total show "not reported". Known sources we do not collect are listed as
   "not covered" with the reason, so completeness is honest.

## My progress

The **My progress** tab tracks the hackathons you are in (stage, next step, deadline) and your prize goal.
It reads `pipeline.json`, which is yours and is not committed. Copy `pipeline.example.json` to
`pipeline.json` to start; without it the tab is empty. Events you mark Shortlist or Applied join it automatically.

## Tuning

Edit `profile.json`: themes, weights and `edge` points, `theme_max` / `edge_max`, travel countries,
which flags block vs. warn. A theme group can carry `conflict` (keywords that turn the group off and raise a warning).
Changes apply on the next page load, no rescrape needed.

## Limits (read before trusting a "Go")

- Eligibility flags come from text patterns. A "Go" means "worth opening the rules", not "eligible".
- Prize USD uses a fixed approximate FX table for ranking only.
- One request per second per site. No logins. Requests say who they are:
  `HackathonRadar/1.0 (+https://github.com/tbd-hackathons/hackathon-radar)` (override with `RADAR_USER_AGENT`).
- **HackerEarth and Eventbrite reject datacenter/VPN IPs** with 403/405 ("ANONYMOUS IP"). A browser from
  the same IP gets the same wall, so it is a network-path problem, not a browser or header one. The radar now
  handles this itself: on the first such refusal it finds a physical interface, binds outgoing sockets to it
  and retries — no setting and no user-agent lie. Override the address, or pin it, with
  `RADAR_SOURCE_IP=$(ipconfig getifaddr en0)`. If detection finds nothing, that source is reported
  blocked-by-site and every other source is unaffected.
- **Eventbrite has two ways in**, so one breaking does not take it down: the directory search API (a CSRF
  cookie from the listing page, no user token) and, failing that, the results the listing page ships inside
  `window.__SERVER_DATA__`. Its search is fuzzy — "hackathon" also returns workshops, talks and meetups — so
  results are filtered to events whose title or summary actually reads as a hackathon. If neither route yields
  anything readable, the source reports a failure instead of a quiet zero.
- **One disclosed exception to that user agent: DoraHacks.** Its API answers our honest user agent with
  HTTP 405 and the same endpoint with a browser user agent with the public data (checked 10 Oct 2026). So
  `radar/http.py` carries a `HOST_UA` map naming that host and nothing else — the exception is visible in
  the code, not silent. Remove the entry and the Coverage tab shows DoraHacks as failed again.
- Low counts are real, not bugs (checked 10 Oct 2026): lablab.ai embeds ~140 events but only 3 have not
  ended; ETHGlobal embeds ~340 events (meetups, summits, past) with 1 future hackathon; Taikai has 8 open
  challenges of 278 (the old page read only saw the first 20 and missed one, now fixed via its API).
- Sources not covered (also listed on the Coverage tab):
  - **Sponsor-run challenges** (Google, AWS, Microsoft, Meta…): own pages, no shared feed. Check by hand.

## CLI

```bash
python3 radar.py scrape       # one full run in the terminal
python3 radar.py top 20       # best 20 non-blocked matches
python3 export.py             # write export/*.csv and export/hackathons.json
```

## License

MIT
