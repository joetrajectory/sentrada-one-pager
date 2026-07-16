#!/usr/bin/env bash
# SessionStart self-heal: runner/pieces/ is gitignored and does not survive a
# container restart. If it comes up empty, pull every render/card PNG back from
# the durable `deliverables` branch so a fresh session is never staring at a
# wiped pieces/ folder. Renders + cards only; data.json/research/QC rebuild from
# research if the chain must re-run. Never blocks the session: any failure is
# swallowed so a network blip or missing branch cannot stop startup.
set +e
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT" || exit 0
# Only act when there are no rendered PNGs on disk (don't disturb active work).
if ls runner/pieces/*/*.png >/dev/null 2>&1; then
  exit 0
fi
timeout 180 python runner/sentrada_runner.py restore --all >/dev/null 2>&1
exit 0
