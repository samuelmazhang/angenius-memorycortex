#!/usr/bin/env python3
"""Full E2E test suite v3 - uses real OpenAPI schemas."""
import json, urllib.request, urllib.error, uuid, subprocess
import psycopg2

BASE = "http://localhost:1880"
PASS, FAIL = 0, 0
results = []

def http(method, path, data=None, params=None, timeout=10):
    url = BASE + path
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)
    if data is not None:
        body = json.dumps(data).encode('utf-8')
        req = urllib.request.Request(url, data=body, method=method,
            headers={'Content-Type': 'application/json'})
    else:
        req = urllib.request.Request(url, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        code = r.getcode()
        body = r.read()
        try:
            return code, json.loads(body)
        except:
            return code, body.decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except:
            return e.code, body.decode('utf-8', errors='replace')
    except Exception as e:
        return 0, str(e)

def test(name, fn):
    global PASS, FAIL
    try:
        result = fn()
        if result is True or (isinstance(result, tuple) and result[0]):
            PASS += 1
            print(f"  PASS {name}")
            results.append((name, 'PASS', None))
            return result
        else:
            FAIL += 1
            detail = result[1] if isinstance(result, tuple) else result
            print(f"  FAIL {name}: {detail}")
            results.append((name, 'FAIL', str(detail)))
            return result
    except Exception as e:
        FAIL += 1
        print(f"  FAIL {name}: EXCEPTION {type(e).__name__}: {e}")
        results.append((name, 'FAIL', f'{type(e).__name__}: {e}'))
        return None

print("="*70)
print("  E2E TEST SUITE v3 - Unified memory-cortex container")
print(f"  Target: {BASE}")
print("="*70)

# ──── [1] Pre-flight ────
print("\n[1] Pre-flight checks")

def t_pf_container():
    r = subprocess.run(['docker', 'ps', '-a', '--filter', 'name=memory-cortex',
                        '--format', '{{.Names}} {{.Status}}'], capture_output=True, text=True)
    return 'Up' in r.stdout
test("container memory-cortex is Up", t_pf_container)

def t_pf_pg():
    dst = psycopg2.connect(host='localhost', port=5432, database='postgres',
                           user='postgres', password='change_me', connect_timeout=5)
    cur = dst.cursor()
    cur.execute('SELECT count(*) FROM memory_chunks')
    n = cur.fetchone()[0]
    dst.close()
    return n > 900
test("PG has migrated data (chunks>900)", t_pf_pg)

# ──── [2] System endpoints ────
print("\n[2] System endpoints")

def t_health():
    code, body = http('GET', '/health')
    if code != 200 or body.get('status') != 'healthy': return f"code={code}"
    return True, body
ok, h = test("/health", t_health)
if ok:
    initial_chunks = h['chunks']
    initial_relations = h['relations']
    print(f"     -> chunks={initial_chunks}, relations={initial_relations}")

test("/stats", lambda: http('GET', '/stats')[0] == 200)
test("/endpoints", lambda: http('GET', '/endpoints')[0] == 200)
test("/storage/health", lambda: http('GET', '/storage/health')[0] == 200)
test("/dashboard", lambda: http('GET', '/dashboard')[0] == 200)
test("/mode", lambda: http('GET', '/mode')[0] == 200)

def t_openapi():
    code, body = http('GET', '/openapi.json')
    if code != 200: return f"code={code}"
    return True, f"{len(body.get('paths', {}))} paths"
test("/openapi.json", t_openapi)

# ──── [3] Chunk CRUD lifecycle ────
print("\n[3] Chunk CRUD lifecycle")
TEST_TAG = f"e2e-test-{uuid.uuid4().hex[:6]}"
TEST_CHUNK_ID = None

def t_create_chunk():
    global TEST_CHUNK_ID
    code, body = http('POST', '/chunks', {
        'content': 'E2E test 2026-07-28: unified container works',
        'type': 'experience', 'importance': 8,
        'tags': [TEST_TAG, 'e2e', 'unified'],
        'metadata': {'test_run_id': TEST_TAG}
    })
    if code != 200: return f"expected 200, got {code}"
    TEST_CHUNK_ID = body['id']
    return True
test("POST /chunks", t_create_chunk)

def t_get_chunk():
    code, body = http('GET', f'/chunks/{TEST_CHUNK_ID}')
    if code != 200: return f"code={code}"
    if not body.get('content', '').startswith('E2E test'):
        return "content mismatch"
    return True
test("GET /chunks/{id}", t_get_chunk)

def t_search_zh():
    code, body = http('POST', '/search', {
        'query': 'unified container E2E test',
        'limit': 3, 'min_similarity': 0.0
    })
    if code != 200: return f"code={code}"
    if not body.get('results'): return "empty"
    found = any(r.get('id') == TEST_CHUNK_ID for r in body['results'])
    return found or f"not in top 3: {[r.get('id', '?')[:18] for r in body['results']]}"
test("POST /search (query hits our chunk)", t_search_zh)

def t_search_exact():
    code, body = http('POST', '/search', {'query': 'unified container works', 'limit': 5})
    if code != 200: return f"code={code}"
    sims = [r['similarity'] for r in body['results']]
    return True, f"top sim={sims[0]:.4f}"
test("POST /search (semantic)", t_search_exact)

def t_search_tag():
    code, body = http('POST', '/search', {'query': 'database', 'limit': 5, 'tags': [TEST_TAG]})
    return code == 200 and bool(body.get('results'))
test("POST /search (tag filter)", t_search_tag)

def t_update_chunk():
    code, body = http('POST', f'/chunks/{TEST_CHUNK_ID}', {
        'content': 'E2E test 2026-07-28: UPDATE test unified container',
        'type': 'experience', 'importance': 9,
        'tags': [TEST_TAG, 'e2e', 'unified', 'updated'],
        'metadata': {'updated': True}
    })
    return code == 200
test("POST /chunks/{id} (update)", t_update_chunk)

def t_search_after_update():
    code, body = http('POST', '/search', {'query': 'UPDATE test unified', 'limit': 3})
    if code != 200: return f"code={code}"
    return any(r.get('id') == TEST_CHUNK_ID for r in body.get('results', []))
test("POST /search after update", t_search_after_update)

# ──── [4] Relations (correct schemas) ────
print("\n[4] Relations")

def t_get_relations():
    # /chunks/{id} doesn't return relations list, but we can use the API directly
    code, body = http('GET', '/chunks', {'limit': 1})
    items = body if isinstance(body, list) else body.get('chunks', [])
    if not items:
        # Try create one first
        c, cb = http('POST', '/chunks', {'content': 'temp for relations test', 'type': 'general'})
        if c != 200: return f"couldn't create temp chunk: {c}"
        cid = cb['id']
    else:
        cid = items[0]['id']
    code, rels = http('GET', f'/relations/{cid}')
    if code != 200: return f"code={code}"
    return True, f"{len(rels) if isinstance(rels, list) else rels.get('count', 0)} relations"
test("GET /relations/{memory_id}", t_get_relations)

def t_temporal_relations():
    # Get 2 chunks first
    c1, b1 = http('POST', '/chunks', {'content': 'temporal test source', 'type': 'general'})
    if c1 != 200: return f"create src: {c1}"
    c2, b2 = http('POST', '/chunks', {'content': 'temporal test target', 'type': 'general'})
    if c2 != 200: return f"create tgt: {c2}"
    code, rel = http('POST', '/relations/temporal', {
        'source_id': b1['id'], 'target_id': b2['id'],
        'relation_type': 'e2e_test', 'valid_from': '2026-07-28T11:00:00Z'
    })
    if code not in (200, 201): return f"code={code}, body={rel}"
    # cleanup
    http('DELETE', f'/chunks/{b1["id"]}')
    http('DELETE', f'/chunks/{b2["id"]}')
    return True
test("POST /relations/temporal", t_temporal_relations)

# ──── [5] Working memory (correct schemas) ────
print("\n[5] Working memory")
SID = f"e2e-{uuid.uuid4().hex[:6]}"

def t_working_set():
    # V5.2.21+: session_id/key/value 都在 JSON body（body-driven 签名）
    code, body = http('POST', '/working/set',
        data={'session_id': SID, 'key': 'user_query', 'value': {'q': 'test', 'count': 42}})
    if code != 200: return f"expected 200, got {code}"
    return True
test("POST /working/set", t_working_set)

def t_working_context():
    code, body = http('GET', f'/working/{SID}/context')
    if code != 200: return f"expected 200, got {code}"
    if not isinstance(body, dict): return f"unexpected: {body}"
    return True, f"context has {len(body)} entries"
test("GET /working/{session_id}/context", t_working_context)

def t_working_append():
    # needs {key,value} or {role,content}
    code, body = http('POST', f'/working/{SID}/append',
        data={'key': 'e2e_event', 'value': {'step': 1, 'event': 'e2e_test'}})
    if code != 200: return f"expected 200, got {code}"
    return True
test("POST /working/{session_id}/append", t_working_append)

# ──── [6] Taxonomy / Audit (correct schemas) ────
print("\n[6] Taxonomy / Audit / Forgetting")

def t_taxonomy():
    code, body = http('GET', '/taxonomy/relations')
    if code != 200: return f"expected 200, got {code}"
    if not isinstance(body, (list, dict)): return f"unexpected: {type(body)}"
    return True
test("GET /taxonomy/relations", t_taxonomy)

test("/forgetting/stats", lambda: http('GET', '/forgetting/stats')[0] == 200)
test("/forgetting/expired", lambda: http('GET', '/forgetting/expired')[0] == 200)

def t_audit():
    code, body = http('GET', '/audit', {'limit': 5})
    if code != 200: return f"expected 200, got {code}"
    return True
test("GET /audit", t_audit)

def t_audit_log():
    # memory_id and action are QUERY params
    code, body = http('POST', '/audit/log',
        params={'memory_id': TEST_CHUNK_ID, 'action': 'e2e_test'})
    if code != 200: return f"expected 200, got {code}"
    return True
test("POST /audit/log", t_audit_log)

# ──── [7] Search variants (correct: GET with q) ────
print("\n[7] Search variants")

def t_search_hybrid():
    code, body = http('GET', '/search/hybrid', params={'q': 'docker cortex takeover', 'limit': 5})
    if code != 200: return f"expected 200, got {code}"
    return True, f"{len(body.get('results', []))} results"
test("GET /search/hybrid", t_search_hybrid)

def t_search_tts():
    code, body = http('GET', '/search/tts', params={'q': 'cortex takeover', 'limit': 3})
    if code != 200: return f"expected 200, got {code}"
    return True, f"{len(body.get('results', []))} results"
test("GET /search/tts", t_search_tts)

test("/procedural/search", lambda: http('GET', '/procedural/search', params={'q': 'test'})[0] == 200)

def t_search_high_limit():
    code, body = http('POST', '/search', {'query': 'memory', 'limit': 50})
    return code == 200
test("POST /search limit=50", t_search_high_limit)

# ──── [8] Advanced (correct schemas) ────
print("\n[8] Advanced endpoints")

def t_episodes_create():
    code, body = http('POST', '/episodes', {
        'session_id': SID,
        'title': 'E2E test episode 2026-07-28',
        'summary': 'E2E test episode',
        'payload': {'test': True}
    })
    if code not in (200, 201): return f"code={code}, body={body}"
    return True
test("POST /episodes (correct schema)", t_episodes_create)

def t_core_upsert():
    code, body = http('POST', '/core/e2e-tester',
        data={'profile': {'name': 'E2E Tester', 'role': 'automation'}},
        params={'source': 'e2e'})
    if code != 200: return f"expected 200, got {code}"
    return True
test("POST /core/{user_id}", t_core_upsert)

test("GET /core/{user_id}", lambda: http('GET', '/core/e2e-tester')[0] == 200)
test("GET /core (list)", lambda: http('GET', '/core')[0] == 200)

def t_hierarchy_create():
    code, body = http('POST', '/hierarchy', {
        'name': 'e2e-test-root',
        'node_type': 'episode',
        'metadata': {'test': True}
    })
    if code not in (200, 201): return f"code={code}, body={body}"
    return True
test("POST /hierarchy (correct schema)", t_hierarchy_create)

test("/access-stats", lambda: http('GET', '/access-stats', {'limit': 5})[0] == 200)
test("/rumination/stats", lambda: http('GET', '/rumination/stats')[0] == 200)
test("/stats/ops", lambda: http('GET', '/stats/ops')[0] == 200)
test("/recommend (scenario)", lambda: http('GET', '/endpoints/recommend/quick')[0] == 200)
test("/history/{memory_id}", lambda: http('GET', f'/history/{TEST_CHUNK_ID}')[0] == 200)
test("/provenance/{memory_id}", lambda: http('GET', f'/provenance/{TEST_CHUNK_ID}')[0] == 200)

# ──── [9] Edge cases ────
print("\n[9] Edge cases")

def t_empty_search():
    code, body = http('POST', '/search', {'query': '', 'limit': 5})
    return code == 200
test("POST /search (empty query)", t_empty_search)

test("GET /chunks/<invalid> -> 404",
     lambda: http('GET', '/chunks/nonexistent-id-12345')[0] == 404)

def t_invalid_json():
    req = urllib.request.Request(BASE + '/chunks',
        data=b'not-valid-json',
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        urllib.request.urlopen(req, timeout=5)
        return False
    except urllib.error.HTTPError as e:
        return e.code in (400, 422)
    except:
        return True
test("POST /chunks (invalid JSON)", t_invalid_json)

def t_concurrent_searches():
    import threading
    results = []
    def worker(i):
        c, _ = http('POST', '/search', {'query': f'memory test {i}', 'limit': 3})
        results.append(c)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()
    return all(r == 200 for r in results)
test("10 concurrent /search requests", t_concurrent_searches)

# ──── [10] Cleanup ────
print("\n[10] Cleanup")

test("DELETE /chunks/{id}",
     lambda: http('DELETE', f'/chunks/{TEST_CHUNK_ID}')[0] in (200, 204))

test("GET /chunks/{id} after delete -> 404",
     lambda: http('GET', f'/chunks/{TEST_CHUNK_ID}')[0] == 404)

# ──── [11] Final state ────
print("\n[11] Final state check")

def t_final():
    code, body = http('GET', '/health')
    if code != 200: return f"code={code}"
    return True, f"final chunks={body.get('chunks')} (started {initial_chunks})"
test("/health after all operations", t_final)

print("\n" + "="*70)
print(f"  RESULTS: {PASS} passed, {FAIL} failed (of {PASS+FAIL})")
print("="*70)

with open('/tmp/e2e_results.json', 'w') as f:
    json.dump({
        'timestamp': '2026-07-28T11:00:00',
        'target': BASE,
        'container': 'memory-cortex (memory-cortex-stack:5.1.0)',
        'total': PASS + FAIL,
        'passed': PASS,
        'failed': FAIL,
        'pass_rate': f"{PASS/(PASS+FAIL)*100:.1f}%" if (PASS+FAIL) > 0 else "n/a",
        'tests': [{'name': r[0], 'status': r[1], 'detail': r[2]} for r in results]
    }, f, indent=2, ensure_ascii=False)
print("\nResults: /tmp/e2e_results.json")

import sys
sys.exit(0 if FAIL == 0 else 1)
