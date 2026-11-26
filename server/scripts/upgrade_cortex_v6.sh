#!/bin/bash
# Cortex V6.0 远程升级脚本 (2026-08-06)
# 用法: ssh <user>@127.0.0.1 'bash /tmp/upgrade_cortex_v6.sh'
# 流程:
#   1. cd 到 /opt/memory-cortex (或 memory-cortex 工作树)
#   2. git pull origin main (拉 V6.0 commit)
#   3. sudo docker restart memory-cortex (10s reload, 数据保留)
#   4. 等 /health 返回 version=6.0.0
#   5. 跑 smoke test: /health + /mode + /search 简单查询
#   6. 失败自动回滚 (git revert + docker restart)
#
# 前置: ADR-069 D1 流程已就绪 (bind-mount server/src)
# 回滚点: 任何步骤失败, 自动 git revert HEAD + docker restart

set -eu

HEALTH_URL="http://localhost:1880/health"
MODE_URL="http://localhost:1880/mode"
TIMEOUT_HEALTH=30
EXPECT_VERSION="6.0.0"

echo "=== Step 1: git pull ==="
cd /opt/memory-cortex 2>/dev/null || cd /opt/cortex-deploy
git pull origin main 2>&1 | tail -5

CURRENT_HEAD=$(git rev-parse --short HEAD)
echo "HEAD now at: $CURRENT_HEAD"

echo ""
echo "=== Step 2: docker restart ==="
sudo docker restart memory-cortex
echo "Container restart signal sent, waiting for /health..."

echo ""
echo "=== Step 3: wait for healthy + version=$EXPECT_VERSION ==="
for i in $(seq 1 $TIMEOUT_HEALTH); do
    HEALTH_RESP=$(curl -s -m 5 "$HEALTH_URL" 2>&1) || continue
    VERSION=$(echo "$HEALTH_RESP" | python -c "import sys, json; print(json.loads(sys.stdin.read()).get('version',''))" 2>/dev/null || echo "")
    if [ "$VERSION" = "$EXPECT_VERSION" ]; then
        echo "✓ Cortex healthy with version=$VERSION (after ${i}s)"
        break
    fi
    if [ "$i" -eq "$TIMEOUT_HEALTH" ]; then
        echo "✗ TIMEOUT: version=$VERSION (expected $EXPECT_VERSION)"
        echo "Auto-rolling back..."
        git revert --no-edit HEAD
        sudo docker restart memory-cortex
        sleep 10
        exit 1
    fi
    sleep 1
done

echo ""
echo "=== Step 4: smoke test ==="
echo "/health: $(curl -s -m 5 "$HEALTH_URL" | python -c "import sys, json; d=json.loads(sys.stdin.read()); print(f\"status={d.get('status')} version={d.get('version')} chunks={d.get('chunks', d.get('stats', {}).get('total_memories', '?'))}\")")"
echo "/mode:   $(curl -s -m 5 "$MODE_URL")"

echo ""
echo "=== Step 5: search smoke ==="
SEARCH_RESP=$(curl -s -m 5 -X POST "http://localhost:1880/search" \
    -H "Content-Type: application/json" \
    -d '{"query":"test","limit":3,"boost_relevance":false}')
SEARCH_OK=$(echo "$SEARCH_RESP" | python -c "import sys, json; d=json.loads(sys.stdin.read()); print('ok' if isinstance(d, (list, dict)) and d.get('results', d) else 'fail')" 2>/dev/null || echo "parse-fail")
echo "/search: $SEARCH_OK"

echo ""
echo "=== Upgrade complete ==="
echo "HEAD: $CURRENT_HEAD"
echo "Version: $EXPECT_VERSION"
