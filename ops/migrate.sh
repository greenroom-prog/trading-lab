#!/usr/bin/env bash
# Consolidate the estate into ~/trading-lab. NON-DESTRUCTIVE.
#
# Copies. Leaves symlinks at the old paths so nothing that references
# them breaks on the first run. Prints the removal commands rather than
# running them -- you delete originals only after ops/status.py is clean.
#
#   bash ops/migrate.sh --dry-run     # default, shows the plan
#   bash ops/migrate.sh --go
set -euo pipefail

LAB="$HOME/trading-lab"
GO=0; [[ "${1:-}" == "--go" ]] && GO=1
run(){ if [[ $GO -eq 1 ]]; then eval "$@"; else echo "  [dry] $*"; fi; }

echo "target: $LAB"
[[ $GO -eq 1 ]] || echo "DRY RUN -- pass --go to execute"
echo

# --- guard: never clobber the expensive caches -------------------------
for c in "$HOME/EDGAR/data/db/states.json"; do
  [[ -f "$c" ]] && echo "cache found: $c ($(stat -c%s "$c") bytes) -- will be copied, never moved"
done
echo

run "mkdir -p '$LAB'/{edgar,settlement,mtf,strategies,video-memory,ops,docs}"

move_repo(){  # $1 source  $2 dest-name
  local src="$1" dst="$LAB/$2"
  [[ -d "$src" ]] || { echo "  skip (absent): $src"; return; }
  [[ -L "$src" ]] && { echo "  skip (already a symlink): $src"; return; }
  echo "$src  ->  $dst"
  run "rsync -a --info=stats1 '$src/' '$dst/'"
  # verify byte-for-byte before touching the original
  run "diff -rq '$src' '$dst' >/dev/null && echo '  verified identical'"
  run "mv '$src' '${src}.premigrate'"
  run "ln -s '$dst' '$src'"
  echo "  symlink left at $src -> $dst"
}

move_repo "$HOME/EDGAR"           "edgar"
move_repo "$HOME/settlement-edge" "settlement"

echo
echo "venv + env -- these do NOT survive a move:"
echo "  .venv holds absolute paths. Recreate, do not copy:"
echo "    cd $LAB/edgar && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
echo "  .env is not committed. Confirm all three keys landed:"
echo "    grep -c . $LAB/edgar/.env    # expect >= 3"
echo
echo "n8n survives: its HTTP nodes call host.docker.internal:8000, which"
echo "is path-independent. But start the API from the NEW directory."
echo
echo "after ops/status.py is clean, remove the originals:"
echo "  rm -rf $HOME/EDGAR.premigrate $HOME/settlement-edge.premigrate"
