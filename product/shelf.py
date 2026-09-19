#!/usr/bin/env python3
"""
Shelf detection from EDGAR filing history.

    python3 shelf.py --cik 1599407
    python3 shelf.py --ticker AIM
    python3 shelf.py --self-test

WHY THIS FILE EXISTS
    state.py computes runway but has no shelf awareness, so the two tags
    a buyer would actually pay for could never fire:

        DILUTION LIKELY   short runway + a shelf on file
        FORCED ACTION     short runway + NO shelf

    A shelf is just an S-3 filing, and every S-3 is already in EDGAR. So
    this reads it rather than asking anyone to add a field.

WHAT THE FORMS MEAN
    S-3        shelf registration. Pre-registers securities so the company
               can sell them later on short notice. Valid 3 years.
    S-3ASR     automatic shelf, for large seasoned issuers. Effective on
               filing, no SEC review, no dollar cap.
    S-1        registration for companies not S-3 eligible - typically
               smaller, and often the only route for a distressed microcap.
    424B5      prospectus supplement: a TAKEDOWN off an existing shelf.
    424B3/B4   other prospectus supplements.

    The distinction that matters:

        an S-3 on file  = they CAN raise, quickly           -> risk
        a recent 424B5  = they ARE raising, right now       -> happening

    A 424B5 is not a dilution risk. It is dilution in progress, and it is
    a materially stronger signal than the shelf that enabled it.

THE THREE-YEAR RULE
    A shelf expires three years after effectiveness. An S-3 from 2019 is
    not a shelf, it is history. Anything that treats it as live will tag
    companies that have no ability to raise at all - which inverts the
    conclusion, because a company that CANNOT issue equity is the one
    facing a forced asset sale.

RATE LIMIT
    SEC allows 10 req/s. This runs one call per company, sequential, with
    a delay. Never parallelise - being blocked is silent.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time

import requests

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SHELF_FORMS = {"S-3", "S-3ASR", "S-3/A", "S-3MEF"}
BACKUP_FORMS = {"S-1", "S-1/A"}
# NOT all 424s are the same, and conflating them was a real bug:
#   424B5  takedown off a shelf - the COMPANY sells NEW shares -> dilution
#   424B2  takedown, usually debt/MTN programs                 -> dilution
#   424B4  IPO/offering priced                                 -> dilution
#   424B3  usually a RESALE prospectus: registers shares an existing
#          holder already owns. The company raises nothing.    -> NOT dilution
#   424B7  also resale, by selling securityholders.            -> NOT dilution
# Tagging a resale as "diluting now" is wrong in a way a buyer catches
# on the first report.
TAKEDOWN_FORMS = {"424B5", "424B2", "424B4"}
RESALE_FORMS = {"424B3", "424B7"}
SHELF_LIFE_DAYS = 365 * 3
TAKEDOWN_RECENT_DAYS = 90
_LAST = [0.0]


def _read_env_file(path: str) -> dict:
    """Parse a .env WITHOUT executing it.

    `set -a; source .env` runs every line through the shell. An unquoted
    value containing a space is parsed as a command and its remainder is
    echoed to the terminal - which is how secrets end up on screen and in
    scrollback. This reads the file as text. Nothing is executed, ever.
    """
    out = {}
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]                       # strip matching quotes
        out[k.strip()] = v
    return out


def env(key: str, default=None):
    """Environment first, then .env files - read, never sourced."""
    if os.environ.get(key):
        return os.environ[key]
    for p in (os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", ".env"),
              os.path.expanduser("~/trading-lab/.env"),
              os.path.expanduser("~/trading-lab/edgar/.env")):
        v = _read_env_file(os.path.abspath(p)).get(key)
        if v:
            return v
    return default


def _ua() -> dict:
    ua = env("EDGAR_UA")
    if not ua:
        raise SystemExit(
            "EDGAR_UA not found in the environment or in .env.\n"
            "  Do NOT `source` the .env - unquoted values get executed\n"
            "  by the shell and printed. This reads the file directly;\n"
            "  just make sure EDGAR_UA is present in it.")
    return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}


def _throttle(min_gap: float = 0.15):
    wait = min_gap - (time.time() - _LAST[0])
    if wait > 0:
        time.sleep(wait)
    _LAST[0] = time.time()


def filings(cik: int, timeout: int = 30) -> list[dict]:
    """Recent filing history for a CIK. One call, throttled."""
    _throttle()
    r = requests.get(SUBMISSIONS.format(cik=int(cik)), headers=_ua(),
                     timeout=timeout)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    rec = r.json().get("filings", {}).get("recent", {})
    forms = rec.get("form", [])
    dates = rec.get("filingDate", [])
    accs = rec.get("accessionNumber", [])
    return [{"form": f, "date": d, "accession": a}
            for f, d, a in zip(forms, dates, accs)]


def assess(rows: list[dict], today: dt.date | None = None) -> dict:
    """Classify shelf capacity from filing history. Pure function - the
    self-test drives it with fixtures, no network."""
    today = today or dt.date.today()
    out = {
        "has_shelf": False, "shelf_form": None, "shelf_date": None,
        "shelf_age_days": None, "shelf_expired": False,
        "has_backup_registration": False,
        "active_takedown": False, "takedown_form": None,
        "takedown_date": None, "takedown_days_ago": None,
        "resale_form": None, "resale_date": None, "resale_days_ago": None,
        "capacity": "unknown", "note": "",
    }
    if not rows:
        out["note"] = "no filing history returned"
        return out

    def newest(forms):
        best = None
        for r in rows:
            if r["form"] not in forms:
                continue
            try:
                d = dt.date.fromisoformat(r["date"])
            except (ValueError, TypeError):
                continue
            if best is None or d > best[0]:
                best = (d, r["form"])
        return best

    sh = newest(SHELF_FORMS)
    if sh:
        age = (today - sh[0]).days
        out.update(shelf_form=sh[1], shelf_date=sh[0].isoformat(),
                   shelf_age_days=age,
                   shelf_expired=age > SHELF_LIFE_DAYS,
                   has_shelf=age <= SHELF_LIFE_DAYS)

    td = newest(TAKEDOWN_FORMS)
    if td:
        ago = (today - td[0]).days
        out.update(takedown_form=td[1], takedown_date=td[0].isoformat(),
                   takedown_days_ago=ago,
                   active_takedown=ago <= TAKEDOWN_RECENT_DAYS)

    rs = newest(RESALE_FORMS)
    if rs:
        out.update(resale_form=rs[1], resale_date=rs[0].isoformat(),
                   resale_days_ago=(today - rs[0]).days)

    bk = newest(BACKUP_FORMS)
    if bk and (today - bk[0]).days <= SHELF_LIFE_DAYS:
        out["has_backup_registration"] = True

    # capacity - the field the report actually consumes
    if out["active_takedown"]:
        out["capacity"] = "raising_now"
        out["note"] = (f"{out['takedown_form']} filed "
                       f"{out['takedown_days_ago']}d ago - company is "
                       f"selling NEW shares")
    elif out["has_shelf"]:
        out["capacity"] = "can_raise"
        if out["resale_form"] and out["resale_days_ago"] <= 180:
            out["note"] = (f"{out['shelf_form']} on file "
                           f"({out['shelf_age_days']}d); also "
                           f"{out['resale_form']} {out['resale_days_ago']}d "
                           f"ago - that is a RESALE by existing holders, "
                           f"not a company raise")
            return out
        out["note"] = (f"{out['shelf_form']} on file, "
                       f"{out['shelf_age_days']}d old, still within 3yr life")
    elif out["shelf_expired"]:
        out["capacity"] = "shelf_expired"
        out["note"] = (f"last shelf {out['shelf_age_days']}d old - EXPIRED. "
                       f"Cannot issue off it without refiling.")
    elif out["resale_form"] and out["resale_days_ago"] <= 180 \
            and not out["has_backup_registration"]:
        out["capacity"] = "resale_only"
        out["note"] = (f"{out['resale_form']} {out['resale_days_ago']}d ago "
                       f"is a RESALE by existing holders - the company "
                       f"raises nothing. No shelf on file.")
    elif out["has_backup_registration"]:
        out["capacity"] = "s1_only"
        out["note"] = "S-1 only, no shelf - slower and subject to SEC review"
    else:
        out["capacity"] = "no_registration"
        out["note"] = ("no shelf and no S-1 - equity raise needs a new "
                       "filing first. Asset sale or restructuring is the "
                       "faster route.")
    return out


def for_cik(cik: int) -> dict:
    return assess(filings(cik))


# ----------------------------------------------------------------------

def self_test() -> int:
    """Fixtures only. Verifies the three-year rule and the raising-now
    distinction without touching the network."""
    today = dt.date(2026, 9, 5)
    fails = []

    def case(name, rows, want_cap):
        got = assess(rows, today)
        ok = got["capacity"] == want_cap
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<34} "
              f"-> {got['capacity']}")
        print(f"          {got['note']}")
        if not ok:
            fails.append(f"{name}: got {got['capacity']} want {want_cap}")

    print("=" * 70)
    print("SHELF LOGIC — fixtures, no network")
    print("=" * 70)
    case("fresh S-3, no takedown",
         [{"form": "S-3", "date": "2025-04-10", "accession": "x"}],
         "can_raise")
    case("S-3 from 2019 (expired)",
         [{"form": "S-3", "date": "2019-02-01", "accession": "x"}],
         "shelf_expired")
    case("S-3 + 424B5 last month",
         [{"form": "S-3", "date": "2024-06-01", "accession": "x"},
          {"form": "424B5", "date": "2026-08-14", "accession": "y"}],
         "raising_now")
    case("S-3 + 424B5 two years ago",
         [{"form": "S-3", "date": "2025-01-05", "accession": "x"},
          {"form": "424B5", "date": "2024-03-01", "accession": "y"}],
         "can_raise")
    case("S-1 only, no shelf",
         [{"form": "S-1", "date": "2025-11-20", "accession": "x"}],
         "s1_only")
    case("nothing on file",
         [{"form": "8-K", "date": "2026-09-01", "accession": "x"},
          {"form": "10-Q", "date": "2026-08-10", "accession": "y"}],
         "no_registration")

    case("424B3 resale only, no shelf",
         [{"form": "424B3", "date": "2026-07-01", "accession": "x"}],
         "resale_only")
    case("424B7 resale only, no shelf",
         [{"form": "424B7", "date": "2026-08-01", "accession": "x"}],
         "resale_only")
    case("424B5 100d ago (outside 90d window)",
         [{"form": "S-3", "date": "2025-01-01", "accession": "x"},
          {"form": "424B5", "date": "2026-05-28", "accession": "y"}],
         "can_raise")
    case("424B2 debt takedown, recent",
         [{"form": "S-3", "date": "2025-01-01", "accession": "x"},
          {"form": "424B2", "date": "2026-08-20", "accession": "y"}],
         "raising_now")

    print()
    print("  boundary: exactly 3 years old")
    edge = assess([{"form": "S-3",
                    "date": (today - dt.timedelta(days=SHELF_LIFE_DAYS)
                             ).isoformat(), "accession": "x"}], today)
    ok = edge["capacity"] == "can_raise" and not edge["shelf_expired"]
    print(f"  {'PASS' if ok else 'FAIL'}  1095 days old is still live")
    if not ok:
        fails.append("boundary")
    edge2 = assess([{"form": "S-3",
                     "date": (today - dt.timedelta(days=SHELF_LIFE_DAYS + 1)
                              ).isoformat(), "accession": "x"}], today)
    ok2 = edge2["capacity"] == "shelf_expired"
    print(f"  {'PASS' if ok2 else 'FAIL'}  1096 days old has expired")
    if not ok2:
        fails.append("boundary+1")

    print()
    print("=" * 70)
    if fails:
        print("FAILED:", fails)
        return 1
    print("ALL PASS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cik", type=int)
    ap.add_argument("--ticker")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()

    cik = a.cik
    if a.ticker and not cik:
        api = os.environ.get("EDGAR_API", "http://localhost:8000")
        try:
            j = requests.get(f"{api}/company/{a.ticker}", timeout=60).json()
            cik = int(j.get("cik") or j.get("state", {}).get("cik"))
        except Exception as e:
            return print(f"could not resolve {a.ticker}: {e}") or 1
    if not cik:
        ap.error("--cik or --ticker required")

    r = for_cik(cik)
    print(f"CIK {cik}")
    for k, v in r.items():
        print(f"  {k:<24} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
