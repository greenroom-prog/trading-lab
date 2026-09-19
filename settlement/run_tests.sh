#!/usr/bin/env bash
# Every test, no network needed. Run this before trusting any change.
set -u
cd "$(dirname "$0")"
fail=0
for t in tests/test_*.py; do
  echo "=== $t"
  python3 "$t" || fail=1
  echo
done
if [ $fail -eq 0 ]; then echo "ALL SUITES PASS"; else echo "SUITES FAILED"; fi
exit $fail
