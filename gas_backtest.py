"""
gas_backtest.py — test the gas-price theses against decades of EIA data.

SCOPE, stated up front because it decides what this can and cannot claim:

  EIA publishes California retail regular WEEKLY, surveyed Monday.
  Kalshi settles its daily contracts on AAA's DAILY average. These are
  different surveys and can differ by 5-30 cents in level.

  Therefore this file tests STRUCTURE, not tomorrow's print:
    - does a one-week spike on a flat base mean-revert?
    - how long does a crude move take to reach the pump, and how much?
    - is there a September seasonal?
  It cannot price a daily strike. That needs the AAA daily series,
  which gas_collector.py accumulates.

    python3 gas_backtest.py --all
    python3 gas_backtest.py --spike --state CA
    python3 gas_backtest.py --lag
    python3 gas_backtest.py --season
"""

from __future__ import annotations

import argparse
import statistics as st
import random
from datetime import datetime

import time
import requests

EIA = "https://api.eia.gov/v2/petroleum/pri/gnd/data/"

# EIA duoarea codes for the 9 states with state-level series
STATES = {"CA": "SCA", "TX": "STX", "FL": "SFL", "NY": "SNY",
          "OH": "SOH", "MN": "SMN", "CO": "SCO", "MA": "SMA", "WA": "SWA"}


def eia_weekly(state="CA", api_key="DEMO_KEY"):
    """Weekly regular retail gasoline, dollars per gallon."""
    duo = STATES.get(state)
    if not duo:
        raise SystemExit(f"EIA has no state series for {state}. "
                         f"Available: {', '.join(STATES)}")
    rows, offset = [], 0
    while True:
        r = requests.get(EIA, params={
            "api_key": api_key,
            "frequency": "weekly",
            "data[0]": "value",
            "facets[duoarea][]": duo,
            "facets[product][]": "EPMR",     # regular gasoline
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            "offset": offset, "length": 5000,
        }, timeout=45)
        if r.status_code == 403:
            raise SystemExit(
                "EIA rejected the key. Get a free one at\n"
                "  https://www.eia.gov/opendata/register.php\n"
                "then add EIA_KEY=... to ~/trading-lab/.env")
        r.raise_for_status()
        batch = r.json().get("response", {}).get("data", [])
        rows += batch
        if len(batch) < 5000:
            break
        offset += 5000
    out = [(d["period"], float(d["value"])) for d in rows
           if d.get("value") not in (None, "")]
    out.sort()
    return out


def pct(a, b):
    return (b - a) / a * 100 if a else 0.0


# ------------------------------------------------------------- thesis 1

def spike_test(s, spike_cents=5.0, flat_cents=3.0, horizon=4):
    """THESIS: a one-week jump on an otherwise flat month fades.

    Condition, evaluated using ONLY data available at decision time:
      last week's change >= spike_cents
      the 4 weeks before that moved < flat_cents in total
    Then measure the following `horizon` weeks.

    Baseline is every other week, so 'it fell afterwards' only counts
    if it fell MORE than gas normally does.
    """
    dates = [d for d, _ in s]
    px = [v for _, v in s]
    fwd_all, fwd_sig, hits = [], [], []

    for i in range(6, len(px) - horizon):
        fwd = pct(px[i], px[i + horizon])
        fwd_all.append(fwd)
        jump = (px[i] - px[i - 1]) * 100
        base = abs(px[i - 1] - px[i - 5]) * 100
        if jump >= spike_cents and base < flat_cents:
            fwd_sig.append(fwd)
            hits.append((dates[i], px[i], jump, base, fwd))

    print("=" * 76)
    print(f"THESIS 1 — spike on a flat base fades ({horizon}w forward)")
    print(f"  trigger: +{spike_cents:.1f}c in one week, prior 4w moved <{flat_cents:.1f}c")
    print("=" * 76)
    if len(fwd_sig) < 10:
        print(f"  only {len(fwd_sig)} occurrences in {len(fwd_all)} weeks — "
              f"too few to conclude.")
        for d, p, j, b, f in hits:
            print(f"    {d}  ${p:.3f}  jump {j:+.1f}c  then {f:+.2f}%")
        return None

    print(f"  baseline: {len(fwd_all)} weeks, mean {st.mean(fwd_all):+.2f}%, "
          f"median {st.median(fwd_all):+.2f}%")
    print(f"  signal:   {len(fwd_sig)} weeks, mean {st.mean(fwd_sig):+.2f}%, "
          f"median {st.median(fwd_sig):+.2f}%")
    down = sum(1 for x in fwd_sig if x < 0)
    downb = sum(1 for x in fwd_all if x < 0)
    print(f"  fell afterwards: signal {down}/{len(fwd_sig)} "
          f"({down/len(fwd_sig)*100:.0f}%)  vs baseline {downb/len(fwd_all)*100:.0f}%")

    edge = st.mean(fwd_sig) - st.mean(fwd_all)
    trials = 10000
    worse = 0
    for _ in range(trials):
        samp = random.sample(fwd_all, len(fwd_sig))
        if abs(st.mean(samp) - st.mean(fwd_all)) >= abs(edge):
            worse += 1
    p = worse / trials
    print(f"\n  edge vs baseline: {edge:+.2f} percentage points")
    print(f"  permutation p = {p:.4f}  ({trials:,} random week-sets)")
    print("  " + ("SURVIVES" if p < 0.05 else
                  "DEAD — random weeks reproduce this."))
    return p


# ------------------------------------------------------------- thesis 2

def crude_weekly(api_key):
    """Weekly WTI spot. Tries EIA first, falls back to FRED.
    Returns [(date, price)] or raises with a clear reason."""
    import os
    # attempt 1: EIA v2 spot route
    try:
        r = requests.get("https://api.eia.gov/v2/petroleum/pri/spt/data/", params={
            "api_key": api_key, "frequency": "weekly", "data[0]": "value",
            "facets[series][]": "RWTC",
            "sort[0][column]": "period", "sort[0][direction]": "asc",
            "length": 5000}, timeout=45)
        if r.ok:
            d = r.json().get("response", {}).get("data", [])
            out = [(x["period"], float(x["value"])) for x in d
                   if x.get("value") not in (None, "")]
            if len(out) > 200:
                return sorted(out), "EIA RWTC"
    except Exception:
        pass
    # attempt 2: FRED weekly WTI
    key = os.getenv("FRED_KEY")
    if key:
        r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                         params={"series_id": "WCOILWTICO", "api_key": key,
                                 "file_type": "json"}, timeout=45)
        if r.ok:
            out = [(o["date"], float(o["value"]))
                   for o in r.json()["observations"] if o["value"] not in (".", "")]
            if len(out) > 200:
                return sorted(out), "FRED WCOILWTICO"
    raise SystemExit("could not fetch weekly crude from EIA or FRED")


def align(gas, crude, max_gap_days=6):
    """Two weekly series rarely share the same anchor day. EIA gas is
    Monday-surveyed; crude weeklies anchor elsewhere. Exact date matching
    returns zero overlap, which is what v1 did. Match each gas week to the
    nearest crude observation within max_gap_days instead."""
    cd = [datetime.fromisoformat(d).date() for d, _ in crude]
    cv = [v for _, v in crude]
    pairs, j = [], 0
    for gdate, gval in gas:
        g = datetime.fromisoformat(gdate).date()
        while j + 1 < len(cd) and abs((cd[j+1] - g).days) <= abs((cd[j] - g).days):
            j += 1
        if abs((cd[j] - g).days) <= max_gap_days:
            pairs.append((gdate, cv[j], gval))
    return pairs


def lag_test(gas, api_key, state="CA"):
    """How long does a crude move take to reach the CA pump, and how much
    of it arrives?"""
    crude, src = crude_weekly(api_key)
    pairs = align(gas, crude)

    print("\n" + "=" * 76)
    print(f"THESIS 2 — crude to {state} pump lag   (crude source: {src})")
    print("=" * 76)
    if len(pairs) < 200:
        print(f"  only {len(pairs)} aligned weeks — not enough. "
              f"gas {len(gas)} weeks, crude {len(crude)} weeks.")
        return
    print(f"  {len(pairs)} aligned weeks, {pairs[0][0]} to {pairs[-1][0]}")
    print(f"  latest: WTI ${pairs[-1][1]:.2f}/bbl   {state} gas ${pairs[-1][2]:.3f}/gal\n")

    c = [p[1] for p in pairs]
    g = [p[2] for p in pairs]
    dc = [pct(c[i-1], c[i]) for i in range(1, len(c))]
    dg = [pct(g[i-1], g[i]) for i in range(1, len(g))]

    def corr(x, y):
        mx, my = st.mean(x), st.mean(y)
        num = sum((a-mx)*(b-my) for a, b in zip(x, y))
        den = (sum((a-mx)**2 for a in x)*sum((b-my)**2 for b in y))**0.5
        return num/den if den else 0.0

    print(f"{'lag wks':>8}{'corr':>9}{'beta':>9}   share of a crude move that arrives")
    best = (0, 0.0)
    for lag in range(0, 11):
        x = dc[:len(dc)-lag] if lag else dc
        y = dg[lag:]
        n = min(len(x), len(y))
        x, y = x[:n], y[:n]
        rr = corr(x, y)
        mx = st.mean(x)
        beta = sum((a-mx)*b for a, b in zip(x, y))/sum((a-mx)**2 for a in x)
        print(f"{lag:>8}{rr:>9.3f}{beta:>9.3f}   {'#'*int(abs(rr)*45)}")
        if abs(rr) > abs(best[1]):
            best = (lag, rr)
    print(f"\n  peak at {best[0]} week(s), corr {best[1]:.3f}")
    if best[0] > 0:
        print(f"  A crude move reaches the {state} pump about {best[0]} week(s) later.")
        print(f"  A daily gas contract settling inside that window cannot")
        print(f"  express a fresh crude view — the pump has not heard yet.")
    else:
        print("  Same-week response. Crude news reaches the pump fast.")


# ------------------------------------------------------------- thesis 3

def season_test(s, state="CA"):
    """Is there a month-of-year pattern in CA gas? September specifically —
    summer-blend changeover happens in the fall."""
    by = {}
    for i in range(1, len(s)):
        m = int(s[i][0][5:7])
        by.setdefault(m, []).append(pct(s[i-1][1], s[i][1]))
    print("\n" + "=" * 76)
    print(f"THESIS 3 — month-of-year pattern in weekly {state} gas changes")
    print("=" * 76)
    names = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    allv = [v for vs in by.values() for v in vs]
    print(f"  all weeks: mean {st.mean(allv):+.3f}%\n")
    for m in range(1, 13):
        v = by.get(m, [])
        if not v:
            continue
        mark = "  <<<" if m == 9 else ""
        print(f"  {names[m-1]}  n={len(v):<4} mean {st.mean(v):+.3f}%  "
              f"median {st.median(v):+.3f}%  down {sum(1 for x in v if x<0)}/{len(v)}{mark}")
    print("\n  PERMUTATION TEST — is the month label doing the work?")
    trials = 10000
    for m in (2, 3, 9, 10, 11, 12):
        v = by.get(m, [])
        if len(v) < 30:
            continue
        obs = st.mean(v) - st.mean(allv)
        worse = sum(1 for _ in range(trials)
                    if abs(st.mean(random.sample(allv, len(v))) - st.mean(allv))
                    >= abs(obs))
        p = worse / trials
        verdict = "REAL" if p < 0.05 else "noise"
        print(f"    {names[m-1]}  effect {obs:+.3f}%/wk   p = {p:.4f}   {verdict}")
    print("\n  A month that fails here is a small sample, not a season.")


def compare_states(api_key, states=None, horizon=4):
    """Run the seasonal and spike tests across every state EIA covers.

    This is the out-of-sample check that matters. A pattern driven by
    physics (blend changeover, driving season) appears in most states.
    A pattern appearing in ONE state is that state's policy or that
    state's crowd, and it will not generalise. A pattern appearing in
    none of them was noise in the first state too.
    """
    states = states or list(STATES)
    names = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    table, spikes = {}, {}

    for stt in states:
        try:
            s = eia_weekly(stt, api_key)
        except SystemExit:
            continue
        except Exception as e:
            print(f"  {stt}: {e}")
            continue
        if len(s) < 300:
            print(f"  {stt}: only {len(s)} weeks, skipping")
            continue
        by = {}
        for i in range(1, len(s)):
            m = int(s[i][0][5:7])
            by.setdefault(m, []).append(pct(s[i-1][1], s[i][1]))
        allv = [v for vs in by.values() for v in vs]
        table[stt] = {m: st.mean(v) for m, v in by.items()}
        table[stt]["all"] = st.mean(allv)
        table[stt]["n"] = len(s)

        # spike test, quiet
        px = [v for _, v in s]
        fa, fs = [], []
        for i in range(6, len(px) - horizon):
            fa.append(pct(px[i], px[i+horizon]))
            if (px[i]-px[i-1])*100 >= 5.0 and abs(px[i-1]-px[i-5])*100 < 3.0:
                fs.append(pct(px[i], px[i+horizon]))
        spikes[stt] = (len(fs), st.mean(fs) if fs else None,
                       st.mean(fa), sum(1 for x in fs if x < 0))
        print(f"  {stt}: {len(s)} weeks loaded", flush=True)
        time.sleep(0.3)

    if not table:
        print("no states loaded")
        return

    print("\n" + "=" * 88)
    print("SEASONAL ACROSS STATES — mean weekly % change by month")
    print("=" * 88)
    hdr = "".join(f"{n:>6}" for n in names)
    print(f"{'state':<7}{hdr}{'all':>8}")
    for stt, row in table.items():
        line = "".join(f"{row.get(m, 0):>6.2f}" for m in range(1, 13))
        print(f"{stt:<7}{line}{row['all']:>8.2f}")

    print("\n  Deviation from each state's own average:")
    print(f"{'state':<7}{hdr}")
    for stt, row in table.items():
        line = "".join(f"{row.get(m,0)-row['all']:>+6.2f}" for m in range(1, 13))
        print(f"{stt:<7}{line}")

    print("\n" + "=" * 88)
    print("AGREEMENT — how many states share each month's sign")
    print("=" * 88)
    n = len(table)
    for m in range(1, 13):
        devs = [row.get(m, 0) - row["all"] for row in table.values()]
        up = sum(1 for d in devs if d > 0)
        agree = max(up, n - up)
        direction = "up" if up > n - up else "down"
        bar = "#" * agree
        note = "  <- unanimous" if agree == n else ""
        print(f"  {names[m-1]}  {agree}/{n} {direction:<5} "
              f"mean dev {st.mean(devs):+.3f}%  {bar}{note}")
    print("\n  A month where states disagree is not a seasonal, whatever")
    print("  one state's p-value says. Physics does not stop at a border.")

    print("\n" + "=" * 88)
    print(f"SPIKE-ON-FLAT-BASE, {horizon}w forward, per state")
    print("=" * 88)
    print(f"{'state':<7}{'n sig':>7}{'signal':>10}{'baseline':>10}{'edge':>9}{'fell':>9}")
    for stt, (n_s, m_s, m_b, dn) in spikes.items():
        if not n_s:
            print(f"{stt:<7}{0:>7}{'--':>10}{m_b:>10.2f}{'--':>9}{'--':>9}")
            continue
        print(f"{stt:<7}{n_s:>7}{m_s:>10.2f}{m_b:>10.2f}{m_s-m_b:>+9.2f}"
              f"{dn}/{n_s:>7}")
    print("\n  Same reading: an edge present in one state and absent in the")
    print("  rest is that state's quirk, not a rule you can size on.")


def main():
    import os
    from pathlib import Path
    try:
        from dotenv import load_dotenv
        load_dotenv(Path.home() / "trading-lab" / ".env")
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="CA")
    ap.add_argument("--spike", action="store_true")
    ap.add_argument("--lag", action="store_true")
    ap.add_argument("--season", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--compare", action="store_true",
                    help="run every EIA state, cross-validate")
    ap.add_argument("--horizon", type=int, default=4)
    ap.add_argument("--key", default=os.getenv("EIA_KEY", "DEMO_KEY"))
    a = ap.parse_args()
    if a.compare:
        print("loading every EIA state series...")
        compare_states(a.key, horizon=a.horizon)
        return
    if not (a.spike or a.lag or a.season or a.all):
        ap.print_help()
        return

    print(f"pulling EIA weekly retail regular for {a.state}...")
    s = eia_weekly(a.state, a.key)
    print(f"{len(s)} weeks, {s[0][0]} to {s[-1][0]}, latest ${s[-1][1]:.3f}\n")

    if a.spike or a.all:
        spike_test(s, horizon=a.horizon)
    if a.lag or a.all:
        lag_test(s, a.key, a.state)
    if a.season or a.all:
        season_test(s, a.state)

    print("\n" + "=" * 76)
    print("WHAT THIS DOES NOT TELL YOU")
    print("=" * 76)
    print("  EIA is weekly and surveyed Monday. Kalshi settles on AAA daily.")
    print("  Different surveys, levels can differ by 5-30 cents. Nothing here")
    print("  prices tomorrow's AAA print. For that, keep running")
    print("  gas_collector.py until it has 20+ consecutive-day changes.")


if __name__ == "__main__":
    main()
