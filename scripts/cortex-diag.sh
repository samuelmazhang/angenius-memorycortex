#!/bin/bash
# cortex-diag.sh
# Diagnostic tool for "why does this chunk have 0/N relations?"
#
# Usage: bash scripts/cortex-diag.sh <chunk_id>
# Example: bash scripts/cortex-diag.sh mem_20260902_000733_893141_fed2b1
#
# Why this script exists (ADR-265):
#   When investigating "missing relations" claims, the natural reflex
#   is to write ad-hoc SQL. We've hit two recurring errors:
#     (1) "column reference \"created_at\" is ambiguous"
#     (2) "cannot cast type jsonb to text[]"
#   Both come from copy-paste SQL with missing table aliases or wrong
#   type casts. This script pre-builds safe SQL templates so we never
#   have to write the SQL ourselves.

set -euo pipefail

CORTEX_URL="${CORTEX_URL:-http://127.0.0.1:1880}"
SSH_KEY="${CORTEX_SSH_KEY:-/e/Users/<user>/.ssh/id_ed25519_lab}"
SSH_USER="${CORTEX_SSH_USER:-<user>@127.0.0.1}"
CONTAINER="${CORTEX_CONTAINER:-memory-cortex}"
DB="${CORTEX_DB:-postgres}"
DB_USER="${CORTEX_DB_USER:-postgres}"

CHUNK_ID="${1:-}"
if [ -z "$CHUNK_ID" ]; then
    echo "Usage: $0 <chunk_id>" >&2
    echo "" >&2
    echo "Examples:" >&2
    echo "  $0 mem_20260902_000733_893141_fed2b1" >&2
    echo "  $0 chunk_abc123def456" >&2
    exit 1
fi

# Validate chunk_id shape: memory_chunks.id is text with pattern
# mem_YYYYMMDD_HHMMSS_<nanos>_<hex>  OR  chunk_<hex>
if [[ ! "$CHUNK_ID" =~ ^(mem_[0-9]{8}_[0-9]{6}_[0-9]+_[a-f0-9]+|chunk_[a-f0-9]+)$ ]]; then
    echo "WARN: chunk_id '$CHUNK_ID' doesn't match expected pattern (mem_YYYYMMDD_HHMMSS_* or chunk_*)" >&2
    echo "WARN: continuing anyway, but the SQL queries below assume the cortex id format" >&2
fi

echo "=== cortex-diag: $CHUNK_ID ==="
echo ""

echo "--- 1. chunk metadata ---"
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_USER" \
  "sudo docker exec $CONTAINER psql -U $DB_USER -d $DB -P pager=off -c \"
    SELECT id, type, source, importance, archived,
           tags::text AS tags_json,
           embedding IS NOT NULL AS has_embedding,
           vector_dims(embedding) AS emb_dim,
           created_at, updated_at
    FROM memory_chunks
    WHERE id = '$CHUNK_ID'
  \""

echo "--- 2. relations involving this chunk (memory_relations) ---"
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_USER" \
  "sudo docker exec $CONTAINER psql -U $DB_USER -d $DB -P pager=off -c \"
    SELECT relation_type, ROUND(strength::numeric, 3) AS strength,
           CASE WHEN memory_id_1 = '$CHUNK_ID' THEN 'outgoing'
                ELSE 'incoming' END AS direction,
           CASE WHEN memory_id_1 = '$CHUNK_ID' THEN memory_id_2
                ELSE memory_id_1 END AS other_chunk
    FROM memory_relations
    WHERE memory_id_1 = '$CHUNK_ID' OR memory_id_2 = '$CHUNK_ID'
    ORDER BY relation_type, direction, strength DESC
  \""

echo "--- 3. relations involving this chunk (memory_links) ---"
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_USER" \
  "sudo docker exec $CONTAINER psql -U $DB_USER -d $DB -P pager=off -c \"
    SELECT link_type, ROUND(strength::numeric, 3) AS strength,
           CASE WHEN source_id = '$CHUNK_ID' THEN 'outgoing'
                ELSE 'incoming' END AS direction,
           CASE WHEN source_id = '$CHUNK_ID' THEN target_id
                ELSE source_id END AS other_chunk
    FROM memory_links
    WHERE source_id = '$CHUNK_ID' OR target_id = '$CHUNK_ID'
    ORDER BY link_type, direction
  \""

echo "--- 4. tag overlap analysis (why tag-shared hook may have missed) ---"
# Build the SQL via a heredoc so the complex JSONB unnest isn't mangled
# by ssh command-line quoting.
TAG_OVERLAP_SQL=$(cat <<EOF
WITH own AS (
  SELECT tags FROM memory_chunks WHERE id = '$CHUNK_ID'
)
SELECT m.id,
       m.tags::text AS tags_json,
       (
         SELECT COUNT(*)::int
         FROM jsonb_array_elements_text(m.tags) t1
         WHERE t1 = ANY(ARRAY(SELECT jsonb_array_elements_text(own.tags)))
       ) AS tag_overlap_with_target
FROM memory_chunks m, own
WHERE m.id <> '$CHUNK_ID'
  AND m.archived = 0
  AND (m.tags ?| ARRAY(SELECT jsonb_array_elements_text(own.tags)))
ORDER BY tag_overlap_with_target DESC, m.created_at DESC
LIMIT 10
;
EOF
)
# Write to a temp file on the remote host, then psql < that file. Using
# stdin pipe rather than -c flag avoids the JSONB nested-quote nightmare.
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_USER" \
  "sudo docker exec -i $CONTAINER psql -U $DB_USER -d $DB -P pager=off" \
  <<< "$TAG_OVERLAP_SQL" | head -30

echo "--- 5. embedding similarity (why similar hook may have missed) ---"
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_USER" \
  "sudo docker exec $CONTAINER psql -U $DB_USER -d $DB -P pager=off -c \"
    SELECT m.id,
           1.0 - (m.embedding <=> (SELECT embedding FROM memory_chunks WHERE id = '$CHUNK_ID')) AS cosine_sim
    FROM memory_chunks m
    WHERE m.id <> '$CHUNK_ID'
      AND m.archived = 0
      AND m.embedding IS NOT NULL
      AND (SELECT embedding FROM memory_chunks WHERE id = '$CHUNK_ID') IS NOT NULL
    ORDER BY m.embedding <=> (SELECT embedding FROM memory_chunks WHERE id = '$CHUNK_ID') ASC
    LIMIT 5
  \""

echo "--- 6. P2 worker state (is the hook actually firing?) ---"
echo -n "Port 1880 worker: "
curl -s --max-time 5 "$CORTEX_URL/admin/maintenance/stats" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['worker_id'] + ' processed=' + str(d['scheduler_state']['total_chunks_processed']) + ' is_running=' + str(d['is_running']) + ' dead=' + str(d['queue_counts']['dead']))"
if curl -s --max-time 3 http://127.0.0.1:1881/admin/maintenance/stats >/dev/null 2>&1; then
    echo -n "Port 1881 worker: "
    curl -s --max-time 5 "http://127.0.0.1:1881/admin/maintenance/stats" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['worker_id'] + ' processed=' + str(d['scheduler_state']['total_chunks_processed']) + ' is_running=' + str(d['is_running']) + ' dead=' + str(d['queue_counts']['dead']))"
fi

echo ""
echo "=== diagnosis hints ==="
echo "  * If step 2 shows 0 rows but step 4 shows chunks with tag_overlap >= 2:"
echo "    -> the chunk's tag-shared hook missed a real match. Force-enqueue:"
echo "       curl -X POST $CORTEX_URL/admin/maintenance/enqueue/$CHUNK_ID?force=true"
echo "  * If step 2 shows 0 rows but step 5 shows neighbors with cosine_sim >= 0.65:"
echo "    -> the chunk's similar hook missed a real match. Same enqueue fix."
echo "  * If both 4 and 5 show nothing above the thresholds:"
echo "    -> this chunk is genuinely isolated. 0 relations is the correct outcome."
echo "  * If step 6 shows dead > 0:"
echo "    -> curl $CORTEX_URL/admin/maintenance/dead  to see which chunks failed"
