#!/usr/bin/env bash
# zvec-memory daily index catchup — freshness belt for the semantic-recall lane.
# Searches self-heal the index 500 rows per call, but a quiet box drifts; this
# keeps the watermark at head so jina-backed recall never goes blind.
# INSTALL (crontab): 0 4 * * * $HOME/zvec-memory/catchup.sh >> $HOME/.hermes/zvec-memory/catchup.log 2>&1
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$DIR/venv/bin/python" - "$DIR" <<'PY'
import json, sys

sys.path.insert(0, sys.argv[1])
import zvec_memory_core as z

for _ in range(40):  # 40 x 500 = 20k rows/run ceiling — runaway brake
    cu = z.index_catchup(limit=500)
    if cu.get("lag", 0) == 0:
        break
z.backfill_jina()
print(json.dumps(z.index_status()))
PY
