#!/bin/bash
# cortex-purge-test-chunks.sh
# Remove test residue (e2e / verify / pytest / probe1-2 / e2e-memtool-XXX)
# from memory_chunks. Designed to be safe (pattern-based, NOT substring) so
# real user notes that happen to mention these tool names are NOT deleted.
#
# Usage:
#   bash scripts/cortex-purge-test-chunks.sh              # default: 8.2:1880
#   CORTEX_URL=http://localhost:1880 bash scripts/...
#   DRY_RUN=1 bash scripts/...                           # show counts, no DELETE
#
# What this deletes (with evidence count from 2026-09-01 run):
#   source IN (verify, verify_651, verify-script, live, verification,
#              user-cli-2026-08-02-mcp-status)               -> 6 rows
#   source LIKE 'verify%' AND type != 'session_summary'    -> 0 rows
#   type = 'test'                                         -> 1 row
#   tags::text LIKE '%release%'                            -> 0 rows
#   source = 'hermes-session-summarizer'                  -> 30 rows (all pytest/verify-e2e)
#   content IN ('probe1', 'probe2')                        -> 86 rows
#   content LIKE 'e2e-memtool-%'                          -> 19 rows (head-anchored)
#   content LIKE '%verify-summarizer%' OR
#   '%test-session-summary-ssbb-%' OR
#   '%session:test-session-%'                             -> 9 rows
#                                                             ----
#                                                              151 rows total
#
# What this DOES NOT delete:
#   - Any chunk where the word 'e2e-memtool' appears INSIDE the body
#     (only chunks that start with 'e2e-memtool-' are deleted)
#   - Real user notes (source='user', type != 'test')
#   - Anything in source='hermes-agent-sprint36-37' (real Sprint 36/37 sessions)

set -euo pipefail

CORTEX_URL="${CORTEX_URL:-http://127.0.0.1:1880}"
SSH_KEY="${CORTEX_SSH_KEY:-/e/Users/<user>/.ssh/id_ed25519_lab}"
SSH_USER="${CORTEX_SSH_USER:-<user>@127.0.0.1}"
CONTAINER="${CORTEX_CONTAINER:-memory-cortex}"
DB="${CORTEX_DB:-postgres}"
DB_USER="${CORTEX_DB_USER:-postgres}"
DRY_RUN="${DRY_RUN:-0}"

echo "=== cortex-purge-test-chunks.sh ==="
echo "target:    $CORTEX_URL (container=$CONTAINER, db=$DB)"
echo "dry_run:   $DRY_RUN (1 = count only, no DELETE)"

# Show pre-state
echo
echo "=== BEFORE: chunks by source (top 20) ==="
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_USER" \
  "sudo docker exec $CONTAINER psql -U $DB_USER -d $DB -c \"SELECT source, COUNT(*) FROM memory_chunks GROUP BY source ORDER BY 2 DESC LIMIT 20\""

echo
echo "=== matching counts BEFORE delete ==="
read -r -d '' QUERY <<'EOF' || true
WITH counts AS (
  SELECT '1. source IN (verify-ish)' AS pattern, COUNT(*) FROM memory_chunks
    WHERE source IN ('verify','verify_651','verify-script','live','verification','user-cli-2026-08-02-mcp-status')
  UNION ALL
  SELECT '2. source LIKE verify% (excluding session_summary)', COUNT(*) FROM memory_chunks
    WHERE source LIKE 'verify%' AND type <> 'session_summary'
  UNION ALL
  SELECT '3. type = test', COUNT(*) FROM memory_chunks WHERE type = 'test'
  UNION ALL
  SELECT '4. tags LIKE %release%', COUNT(*) FROM memory_chunks WHERE tags::text LIKE '%release%'
  UNION ALL
  SELECT '5. source = hermes-session-summarizer', COUNT(*) FROM memory_chunks
    WHERE source = 'hermes-session-summarizer'
  UNION ALL
  SELECT '6. content IN (probe1, probe2)', COUNT(*) FROM memory_chunks
    WHERE content IN ('probe1','probe2')
  UNION ALL
  SELECT '7. content LIKE e2e-memtool-% (head-anchored)', COUNT(*) FROM memory_chunks
    WHERE content LIKE 'e2e-memtool-%'
  UNION ALL
  SELECT '8. content LIKE %verify-summarizer% OR %test-session-summary-ssbb-% OR %session:test-session-%', COUNT(*) FROM memory_chunks
    WHERE content LIKE '%verify-summarizer%'
       OR content LIKE '%test-session-summary-ssbb-%'
       OR content LIKE '%session:test-session-%'
)
SELECT * FROM counts ORDER BY pattern;
EOF

ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_USER" \
  "sudo docker exec $CONTAINER psql -U $DB_USER -d $DB -c \"$QUERY\""

if [ "$DRY_RUN" = "1" ]; then
  echo
  echo "[DRY RUN] no DELETE performed. Re-run without DRY_RUN=1 to apply."
  exit 0
fi

# Confirmation prompt
echo
echo "Proceed with DELETE? (type 'yes' to continue)"
read -r CONFIRM
if [ "$CONFIRM" != "yes" ]; then
  echo "Aborted by user."
  exit 1
fi

# Generate the SQL
read -r -d '' DELETE_SQL <<'EOF' || true
DELETE FROM memory_chunks WHERE source IN ('verify','verify_651','verify-script','live','verification','user-cli-2026-08-02-mcp-status');
DELETE FROM memory_chunks WHERE source LIKE 'verify%' AND type <> 'session_summary';
DELETE FROM memory_chunks WHERE type = 'test';
DELETE FROM memory_chunks WHERE tags::text LIKE '%release%';
DELETE FROM memory_chunks WHERE source = 'hermes-session-summarizer';
DELETE FROM memory_chunks WHERE content IN ('probe1','probe2');
DELETE FROM memory_chunks WHERE content LIKE 'e2e-memtool-%';
DELETE FROM memory_chunks WHERE content LIKE '%verify-summarizer%';
DELETE FROM memory_chunks WHERE content LIKE '%test-session-summary-ssbb-%';
DELETE FROM memory_chunks WHERE content LIKE '%session:test-session-%';
EOF

echo
echo "=== running DELETEs ==="
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_USER" \
  "sudo docker exec -i $CONTAINER psql -U $DB_USER -d $DB" \
  <<< "$DELETE_SQL"

echo
echo "=== AFTER: total chunks + vector count ==="
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_USER" \
  "sudo docker exec $CONTAINER psql -U $DB_USER -d $DB -c \"SELECT COUNT(*) AS total_chunks, COUNT(*) FILTER (WHERE archived=0) AS active, COUNT(*) FILTER (WHERE archived=1) AS archived FROM memory_chunks\""

echo
echo "=== /stats endpoint ==="
curl -s "$CORTEX_URL/stats" | python3 -m json.tool

echo
echo "Done. Re-run with DRY_RUN=1 to verify any future matches."
