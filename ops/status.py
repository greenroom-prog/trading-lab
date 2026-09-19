#!/usr/bin/env python3
"""
What is present, runnable, and blocked. Run after migrating.

MAP.md drifts. This does not -- it reads the filesystem and the live
services. Where the two disagree, this wins.

    python3 ops/status.py
    python3 ops/status.py --net     # also probe :8000
"""
from __future__ import annotations
import argparse, os, sys
from pathlib import Path

LAB = Path(os.environ.get("LAB", Path.home() / "trading-lab"))

# (path, label, blocking?) -- blocking means START_HERE step 1 needs it
CHECKS = [
    ("edgar/edgar/state.py",            "EDGAR state engine (VALIDATED)", True),
    ("edgar/api/server.py",             "EDGAR API :8000", True),
    ("edgar/scripts/healthcheck.py",    "14 assertions", False),
    ("edgar/scripts/event_study.py",    "event study", False),
    ("edgar/scripts/market_adjust.py",  "benchmark adjustment", False),
    ("edgar/edgar/shorts.py",           "borrow INFERENCE (not confirmation)", False),
    ("edgar/.env",                      ".env (3 keys)", True),
    ("edgar/data/db/states.json",       "event-study cache (hours to rebuild)", False),
    ("settlement/carry/economics.py",   "carry economics", False),
    ("settlement/carry/termstructure.py","term structure test", False),
    ("settlement/carry/angles.py",      "angle ranking", False),
    ("settlement/carry/scan.py",        "scanner (HTTP UNTESTED)", False),
    ("mtf/mtf.py",                      "MTF harness", False),
    ("mtf/test_mtf.py",                 "MTF known-cases", False),
    ("video-memory/gate_test.py",       "vision gate (UNRUN)", False),
    ("ops/borrow_check.py",             "borrow check -- TONIGHT", True),
    ("START_HERE.md",                   "the call", False),
    ("KILLS.md",                        "kill register", False),
]

def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--net", action="store_true")
    a = ap.parse_args()
    print(f"lab: {LAB}\n")
    missing_blocking = []
    for rel, label, blocking in CHECKS:
        p = LAB / rel
        ok = p.exists()
        mark = "ok " if ok else ("!! " if blocking else "-- ")
        print(f"  {mark} {label:<42} {rel}")
        if not ok and blocking:
            missing_blocking.append(rel)

    print()
    # Ollama models -- the vision gate is blocked without one
    try:
        import subprocess
        out = subprocess.run(["ollama", "list"], capture_output=True,
                             text=True, timeout=10).stdout
        vis = [l.split()[0] for l in out.splitlines()[1:]
               if "-vl" in l or "vision" in l or "llava" in l]
        print(f"  ollama vision models: {vis or 'NONE -- video-memory gate BLOCKED'}")
    except Exception:
        print("  ollama: not reachable")

    if a.net:
        try:
            import requests
            r = requests.get("http://localhost:8000/health", timeout=15)
            print(f"  EDGAR API /health -> HTTP {r.status_code}")
        except Exception as e:
            print(f"  EDGAR API down ({type(e).__name__}) "
                  f"-- start: cd {LAB}/edgar && python3 api/server.py")

    print()
    if missing_blocking:
        print("BLOCKED. Missing pieces needed for START_HERE step 1:")
        for m in missing_blocking:
            print(f"   {m}")
        return 1
    print("Step 1 is unblocked. Run:")
    print(f"   cd {LAB} && python3 ops/borrow_check.py --probe")
    print(f"   cd {LAB} && python3 ops/borrow_check.py --top 25")
    return 0

if __name__ == "__main__":
    sys.exit(main())
