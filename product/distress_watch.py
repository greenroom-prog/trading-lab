#!/usr/bin/env python3
"""
Distress Watch — the EDGAR signal as a daily product.

    python3 distress_watch.py                    # today's report to screen
    python3 distress_watch.py --html out.html    # styled, emailable
    python3 distress_watch.py --csv out.csv      # for a fund's own pipeline
    python3 distress_watch.py --json out.json    # for an API consumer

WHAT THIS IS
    The trade is blocked: the direction is short, the names are microcaps,
    and locating borrow on them has never been confirmed. But the FINDING
    is validated - 26,287 8-K events, monotonic across two credit regimes,
    survives benchmark adjustment.

    A validated finding you cannot trade is still worth money to someone
    who can, or to someone who does not want to trade it at all:

      credit analysts     who is running out of money, before the market says so
      distressed funds    who will be forced to sell assets or restructure
      short sellers       ones who already have borrow you do not
      journalists         a ranked list of companies quietly running out of runway
      lenders / vendors   counterparty risk on people who owe them

    None of them need you to place a trade. They need the LIST.

WHAT IT ADDS BEYOND "dying or thriving"
    Same runway math, pointed at questions with longer horizons, where
    borrow and spread stop mattering:

      DILUTION RISK    runway short + shelf registration on file
                       -> equity raise likely -> existing holders diluted
      FORCED ACTION    runway short + no shelf + debt due
                       -> asset sale, restructuring, or worse
      REFI WINDOW      leverage high + credit conditions open
                       -> can refinance NOW, may not in six months
      COUNTERPARTY     runway under one quarter
                       -> do not extend them terms

    These are not 20-day trades. They are states that persist for quarters.

FIELD NAMES
    Built against the documented /watch and /company shapes. Response
    shapes drift. --probe checks every field this depends on and names
    the missing ones instead of silently emitting empty columns.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys

import requests

import shelf as shelf_mod

API = shelf_mod.env("EDGAR_API", "http://localhost:8000")
TIMEOUT = 180

# CONFIRMED against a live /watch?score=true response 2026-09-05:
#   filed form company cik url ticker solvency runway_quarters gate
# runway_quarters and `gate` come back on /watch directly, so the slow
# per-company call is only needed for leverage detail on survivors.
WATCH_FIELDS = ["filed", "form", "company", "cik", "url", "ticker",
                "solvency", "runway_quarters", "gate"]
STATE_FIELDS = ["cash", "burn", "debt_to_equity", "interest_coverage",
                "revenue_growth"]

# `solvency` values seen live. "n/a" means XBRL gave no usable figures -
# that is ABSENT data, not healthy data, and must never be read as safe.
KEEP = {"forced", "pressured"}
UNKNOWN = {"n/a", "", None}


def get(path, **params):
    r = requests.get(f"{API}{path}", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


# ----------------------------------------------------------------------

def probe() -> int:
    """Name every field that is missing rather than emitting blank columns."""
    print(f"PROBE {API}")
    try:
        h = requests.get(f"{API}/health", timeout=60)
        print(f"  /health -> HTTP {h.status_code}")
    except Exception as e:
        print(f"  /health unreachable: {type(e).__name__}")
        print(f"  start it: cd edgar && python3 api/server.py")
        return 2

    try:
        w = get("/watch", days=5, score="true")
    except Exception as e:
        print(f"  /watch failed: {type(e).__name__} {e}")
        return 2

    fil = w.get("filings", [])
    print(f"  /watch -> {w.get('count', len(fil))} filings")
    if not fil:
        print("  no filings in the window. Try --days 10.")
        return 1

    s = fil[0]
    print(f"  filing keys: {sorted(s)}")
    missing = [f for f in WATCH_FIELDS if f not in s]
    if missing:
        print(f"  MISSING from /watch: {missing}")
    cond_key = next((k for k in SCORED_FIELDS if k in s), None)
    print(f"  condition field: {cond_key or 'NONE FOUND -- score=true may '
                                           'not be returning a condition'}")

    tick = s.get("ticker")
    if tick:
        try:
            c = get(f"/company/{tick}")
            print(f"  /company/{tick} top-level keys: {sorted(c)}")
            st = c.get("state", {})
            print(f"  state keys: {sorted(st)}")
            miss = [f for f in STATE_FIELDS if f not in st]
            if miss:
                print(f"  MISSING from state: {miss}")
                print("  -> those columns will be blank. Map them in "
                      "FIELD_MAP below once you see the real names.")
        except Exception as e:
            print(f"  /company failed: {type(e).__name__} {e}")
    print("\nPROBE DONE. Fix anything marked MISSING before shipping "
          "this to a buyer.")
    return 0


# ----------------------------------------------------------------------

def condition_of(row: dict) -> str:
    v = row.get("solvency") or row.get("condition") or ""
    return str(v).strip().lower()


def classify(state: dict, macro: dict) -> list[str]:
    """The product layer. Same runway math, longer-horizon questions.

    Order matters. Macro first: a company that cannot refinance has no
    options however clean its balance sheet is.
    """
    tags = []
    runway = state.get("runway_quarters")
    cap = state.get("shelf_capacity")          # from shelf.py
    d2e = state.get("debt_to_equity")
    cov = state.get("interest_coverage")
    growth = state.get("revenue_growth")
    shelf = state.get("has_shelf") or state.get("shelf_registration")
    credit = (macro.get("credit") or "").lower()

    if runway is not None:
        if runway < 1:
            tags.append("COUNTERPARTY RISK — under one quarter of cash")
        elif runway < 4:
            tags.append("FUNDING NEEDED — under four quarters")

    if runway is not None and runway < 4:
        if cap == "raising_now":
            tags.append("DILUTING NOW — shelf takedown filed; company is "
                        "selling new shares")
        elif cap == "can_raise":
            tags.append("DILUTION LIKELY — live shelf on file, can issue "
                        "on short notice")
        elif cap == "shelf_expired":
            tags.append("FORCED ACTION — shelf EXPIRED; cannot issue "
                        "without refiling first")
        elif cap == "s1_only":
            tags.append("SLOW RAISE ONLY — S-1 route, subject to SEC "
                        "review; weeks not days")
        elif cap == "resale_only":
            tags.append("FORCED ACTION — only a resale prospectus on file; "
                        "existing holders selling, company raises nothing")
        elif cap == "no_registration":
            tags.append("FORCED ACTION — no shelf, no S-1; asset sale or "
                        "restructuring is the faster route")

    if credit == "stressed":
        tags.append("NO EXIT — credit stressed, refinancing closed")
    elif credit == "open" and ((d2e or 0) > 2 or (cov is not None and cov < 1.5)):
        tags.append("REFI WINDOW — levered but credit is open NOW")

    if growth is not None and growth > 0 and runway is not None and runway < 4:
        tags.append("REPOSITIONING — revenue growing while cash-constrained")

    return tags


def build(days: int, limit: int) -> dict:
    macro = {}
    try:
        macro = get("/macro")
    except Exception as e:
        print(f"  warning: /macro failed ({type(e).__name__}); "
              f"macro rules suppressed", file=sys.stderr)

    w = get("/watch", days=days, score="true")
    rows, seen, unknown = [], set(), 0
    for f in w.get("filings", []):
        t = f.get("ticker")
        cond = condition_of(f)
        if not t or t in seen:
            continue
        if cond in UNKNOWN:
            unknown += 1          # counted, never treated as healthy
            continue
        if cond not in KEEP:
            continue
        seen.add(t)
        st = {}
        try:
            st = (get(f"/company/{t}").get("state") or {})
        except Exception:
            pass                  # leverage detail missing; runway still valid
        # shelf capacity, read straight from EDGAR filing history
        sh = {}
        cik = f.get("cik")
        if cik:
            try:
                sh = shelf_mod.for_cik(int(cik))
                st["shelf_capacity"] = sh.get("capacity")
            except Exception:
                pass              # absent, never guessed
        rows.append({
            "ticker": t,
            "company": f.get("company", ""),
            "condition": cond,
            "filed": f.get("filed", ""),
            "form": f.get("form", ""),
            "gate": f.get("gate", ""),
            "shelf_capacity": sh.get("capacity", "unknown"),
            "shelf_note": sh.get("note", ""),
            # /watch carries runway directly; fall back to /company
            "runway_quarters": (f.get("runway_quarters")
                                if f.get("runway_quarters") is not None
                                else st.get("runway_quarters")),
            "cash": st.get("cash"),
            "burn": st.get("burn"),
            "debt_to_equity": st.get("debt_to_equity"),
            "interest_coverage": st.get("interest_coverage"),
            "tags": classify(st, macro),
            "url": f.get("url", ""),
        })
        if len(rows) >= limit:
            break

    rows.sort(key=lambda r: (r["runway_quarters"] is None,
                             r["runway_quarters"] or 999))
    return {"date": dt.date.today().isoformat(), "macro": macro,
            "count": len(rows), "screened": w.get("count", 0),
            "unknown": unknown, "companies": rows}


# ----------------------------------------------------------------------

def to_text(rep: dict) -> str:
    m = rep.get("macro", {})
    out = [f"DISTRESS WATCH — {rep['date']}",
           f"credit: {m.get('credit','?')}   policy: {m.get('policy','?')}",
           f"{rep['count']} constrained of {rep.get('screened','?')} filings"
           f"   ({rep.get('unknown',0)} had no usable XBRL)",
           "=" * 72]
    for r in rep["companies"]:
        rq = r["runway_quarters"]
        out.append(f"\n{r['ticker']:<7} {r['company'][:44]:<44} "
                   f"[{r['condition']}]")
        out.append(f"        runway {rq if rq is not None else '?'} quarters"
                   f"   d/e {r['debt_to_equity']}   "
                   f"coverage {r['interest_coverage']}")
        if r.get("gate"):
            out.append(f"        gate: {r['gate']}")
        if r.get("shelf_note"):
            out.append(f"        shelf: {r['shelf_note']}")
        for t in r["tags"]:
            out.append(f"        - {t}")
        if r["url"]:
            out.append(f"        {r['url']}")
    out += ["", "=" * 72,
            "Condition is computed from SEC XBRL. It describes state, not",
            "direction, and is not investment advice.",
            "Method: 26,287 8-K events, two credit regimes, benchmark-adjusted."]
    return "\n".join(out)


def to_html(rep: dict) -> str:
    m = rep.get("macro", {})
    rows = []
    for r in rep["companies"]:
        tags = "".join(f"<li>{t}</li>" for t in r["tags"])
        rq = r["runway_quarters"]
        rows.append(f"""
        <tr><td class=tk>{r['ticker']}<div class=co>{r['company'][:50]}</div></td>
        <td class=num>{rq if rq is not None else '—'}</td>
        <td class=num>{r['debt_to_equity'] if r['debt_to_equity'] is not None else '—'}</td>
        <td class=num>{r['interest_coverage'] if r['interest_coverage'] is not None else '—'}</td>
        <td><span class=cond>{r['condition']}</span><ul>{tags}</ul></td></tr>""")
    return f"""<!doctype html><meta charset=utf-8>
<title>Distress Watch {rep['date']}</title>
<style>
 body{{font:15px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;
   max-width:1000px;margin:36px auto;padding:0 20px;color:#1a1a1a}}
 h1{{font-size:24px;margin:0 0 4px}}
 .sub{{color:#666;margin-bottom:24px;font-size:14px}}
 table{{border-collapse:collapse;width:100%}}
 th{{text-align:left;font-size:12px;text-transform:uppercase;
   letter-spacing:.06em;color:#888;border-bottom:2px solid #e5e5e5;padding:8px}}
 td{{border-bottom:1px solid #eee;padding:12px 8px;vertical-align:top}}
 .tk{{font-weight:600;font-family:ui-monospace,monospace}}
 .co{{font-weight:400;font-size:12px;color:#777;font-family:inherit}}
 .num{{font-family:ui-monospace,monospace;text-align:right;width:90px}}
 .cond{{font-size:11px;text-transform:uppercase;letter-spacing:.06em;
   background:#f2f2f2;padding:2px 7px;border-radius:3px}}
 ul{{margin:8px 0 0;padding-left:18px;font-size:13px;color:#444}}
 li{{margin:2px 0}}
 footer{{margin-top:32px;padding-top:16px;border-top:1px solid #e5e5e5;
   font-size:12px;color:#888}}
</style>
<h1>Distress Watch</h1>
<div class=sub>{rep['date']} &middot; credit {m.get('credit','?')} &middot;
 policy {m.get('policy','?')} &middot; {rep['count']} constrained of {rep.get('screened','?')} filings
 &middot; {rep.get('unknown',0)} without usable XBRL</div>
<table><tr><th>Company</th><th class=num>Runway<br>(qtrs)</th>
<th class=num>D/E</th><th class=num>Cov.</th><th>Condition</th></tr>
{''.join(rows)}</table>
<footer>Condition computed from SEC XBRL filings. Describes financial
state, not price direction. Not investment advice.<br>
Method: 26,287 8-K events across two credit regimes, benchmark-adjusted.
</footer>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--html"); ap.add_argument("--csv"); ap.add_argument("--json")
    a = ap.parse_args()

    if a.probe:
        return probe()

    rep = build(a.days, a.limit)
    if not rep["count"]:
        print("No constrained companies filed in the window.")
        print("If that is surprising run --probe: an empty list and a "
              "renamed field look identical from here.")
        return 0

    print(to_text(rep))

    if a.html:
        open(a.html, "w").write(to_html(rep)); print(f"\nwrote {a.html}")
    if a.json:
        json.dump(rep, open(a.json, "w"), indent=2, default=str)
        print(f"wrote {a.json}")
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(["date", "ticker", "company", "condition",
                         "runway_quarters", "debt_to_equity",
                         "interest_coverage", "shelf_capacity",
                         "tags", "filing_url"])
            for r in rep["companies"]:
                wr.writerow([rep["date"], r["ticker"], r["company"],
                             r["condition"], r["runway_quarters"],
                             r["debt_to_equity"], r["interest_coverage"],
                             r.get("shelf_capacity", ""),
                             " | ".join(r["tags"]), r["url"]])
        print(f"wrote {a.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
