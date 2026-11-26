#!/bin/bash
# cortex-backfill-relations.sh
# Enqueue every existing chunk into the P2 scheduler so the on_process_complete
# hook (_sched_record_similar_relations) generates memory_relations rows
# retroactively for the historical corpus.
#
# Usage:
#   bash scripts/cortex-backfill-relations.sh            # default: 8.2:1880
#
# Why: the P2 scheduler only generates relations for NEW chunks (it was
#      enabled in v6.5.0 P2 but ran for ~150 chunks total). The existing
#      2540 historical chunks have no 'similar' relations in memory_relations
#      beyond the v6.3.x-era /merge-duplicates snapshot. This script brings
#      them up to date.
#
# Cost: ~2540 enqueue calls -> ~2540 P2 worker ticks -> ~12700 memory_relations
#      INSERTs. At HNSW + SKIP LOCKED scale, this completes in ~10 min.
#      No idempotency concern: enqueue uses INSERT ... ON CONFLICT DO NOTHING,
#      relation INSERT uses ON CONFLICT (memory_id_1, memory_id_2) DO UPDATE
#      (overwrites strength with the freshest cosine similarity).

set -euo pipefail

CORTEX_URL="${CORTEX_URL:-http://127.0.0.1:1880}"
SSH_KEY="${CORTEX_SSH_KEY:-/e/Users/<user>/.ssh/id_ed25519_lab}"
SSH_USER="${CORTEX_SSH_USER:-<user>@127.0.0.1}"
CONTAINER="${CORTEX_CONTAINER:-memory-cortex}"
DB="${CORTEX_DB:-postgres}"
DB_USER="${CORTEX_DB_USER:-postgres}"

echo "=== cortex-backfill-relations.sh ==="
echo "target: $CORTEX_URL (container=$CONTAINER, db=$DB)"

# Count chunks
TOTAL=$(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_USER" \
  "sudo docker exec $CONTAINER psql -U $DB_USER -d $DB -t -A -c 'SELECT COUNT(*) FROM memory_chunks WHERE archived=0'" | tr -d ' \r')
echo "active chunks to backfill: $TOTAL"

# Snapshot before
BEFORE_MAIN=$(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_USER" \
  "sudo docker exec $CONTAINER psql -U $DB_USER -d $DB -t -A -c 'SELECT COUNT(*) FROM memory_relations'" | tr -d ' \r')
BEFORE_SIM=$(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_USER" \
  "sudo docker exec $CONTAINER psql -U $DB_USER -d $DB -t -A -c \"SELECT COUNT(*) FROM memory_relations WHERE relation_type='similar'\"" | tr -d ' \r')
echo "BEFORE: total=$BEFORE_MAIN, similar=$BEFORE_SIM"

# Get all active chunk IDs (one per line)
echo
echo "=== fetching active chunk_ids ==="
IDS=$(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_USER" \
  "sudo docker exec $CONTAINER psql -U $DB_USER -d $DB -t -A -F'|' -c \"SELECT id FROM memory_chunks WHERE archived=0 ORDER BY created_at\"")
N_IDS=$(echo "$IDS" | wc -l | tr -d ' \r')
echo "ids to enqueue: $N_IDS"

# Enqueue one by one. The P2 scheduler batch-flushes every 0.2s, so
# each enqueue is fast (in-memory append).
echo
echo "=== enqueueing (one POST per chunk_id) ==="
START_TS=$(date +%s)
COUNT=0
FAILED=0
for cid in $IDS; do
  R=$(curl -s -X POST "$CORTEX_URL/admin/maintenance/enqueue/${cid}?force=true")
  if echo "$R" | grep -q '"status": "queued"'; then
    COUNT=$((COUNT + 1))
  else
    FAILED=$((FAILED + 1))
    [ "$FAILED" -le 3 ] && echo "  FAILED $cid: $R"
  fi
  if [ $((COUNT % 100)) -eq 0 ] && [ "$COUNT" -gt 0 ]; then
    ELAPSED=$(($(date +%s) - START_TS))
    echo "  $COUNT / $N_IDS enqueued (${ELAPSED}s elapsed)"
  fi
done
END_TS=$(date +%s)
echo
echo "=== enqueue done: $COUNT queued, $FAILED failed ($((END_TS - START_TS))s) ==="

# Wait for P2 worker to drain
echo
echo "=== waiting for P2 worker to drain queue ==="
for i in $(seq 1 60); do
  sleep 10
  STATS=$(curl -s "$CORTEX_URL/admin/maintenance/stats")
  PENDING=$(echo "$STATS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['queue_counts']['pending']+d['queue_counts']['processing'])" 2>/dev/null || echo "?")
  PROCESSED=$(echo "$STATS" | python3 -c "import sys,json; print(json.load(sys.stdin)['scheduler_state']['total_chunks_processed'])" 2>/dev/null || echo "?")
  echo "  t=${i}*10s pending=$PENDING total_processed=$PROCESSED"
  if [ "$PENDING" = "0" ]; then break; fi
done

# Snapshot after
AFTER_MAIN=$(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_USER" \
  "sudo docker exec $CONTAINER psql -U $DB_USER -d $DB -t -A -c 'SELECT COUNT(*) FROM memory_relations'" | tr -d ' \r')
AFTER_SIM=$(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_USER" \
  "sudo docker exec $CONTAINER psql -U $DB_USER -d $DB -t -A -c \"SELECT COUNT(*) FROM memory_relations WHERE relation_type='similar'\"" | tr -d ' \r')
echo
echo "=== result ==="
echo "BEFORE: total=$BEFORE_MAIN, similar=$BEFORE_SIM"
echo "AFTER:  total=$AFTER_MAIN,  similar=$AFTER_SIM"
echo "DELTA:  total=$((AFTER_MAIN - BEFORE_MAIN)), similar=$((AFTER_SIM - BEFORE_SIM))"
