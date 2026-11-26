"""V7 dev schema bootstrap: 创建 cortex_v7_dev schema + 从 V6 复制 chunks + relations.

只复制 V7 评估需要的表 (memory_chunks + memory_relations), 不带 forgetting_log / access_stats 等.
幂等: 多次跑结果一致 (CREATE IF NOT EXISTS + TRUNCATE then INSERT).
"""

import os
import sys
import psycopg2
import psycopg2.extras

PG_HOST = "127.0.0.1"
PG_PORT = 5432
PG_USER = "postgres"
PG_PASS = "change_me"
PG_DB = "postgres"
SCHEMA = "cortex_v7_dev"


def main():
    print(f"Connecting to {PG_HOST}:{PG_PORT} db={PG_DB} user={PG_USER}...")
    conn = psycopg2.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, dbname=PG_DB,
        connect_timeout=10,
    )
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        # 1. 建 schema (幂等)
        print(f"[1/4] CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        conn.commit()

        # 2. 在新 schema 里建需要的表 (复用 V6 schema, 简化版本)
        # 完整复制 V6 schema
        print(f"[2/4] CREATE TABLE in {SCHEMA} (memory_chunks + memory_relations)")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.memory_chunks (
                id TEXT PRIMARY KEY,
                content TEXT,
                type TEXT,
                importance INTEGER DEFAULT 5,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                tags JSONB DEFAULT '[]'::jsonb,
                archived INTEGER DEFAULT 0,
                tier TEXT DEFAULT 'short_term',
                embedding TEXT,
                metadata JSONB DEFAULT '{{}}'::jsonb
            )
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.memory_relations (
                id TEXT PRIMARY KEY,
                memory_id_1 TEXT NOT NULL,
                memory_id_2 TEXT NOT NULL,
                relation_type TEXT DEFAULT 'implicit',
                strength REAL DEFAULT 0.0,
                co_occurrence_count INTEGER DEFAULT 1,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ
            )
        """)
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{SCHEMA}_rel_1 ON {SCHEMA}.memory_relations(memory_id_1)")
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{SCHEMA}_rel_2 ON {SCHEMA}.memory_relations(memory_id_2)")
        conn.commit()

        # 3. 复制数据 (从 public schema, V5/V6 默认用 public)
        print(f"[3/4] TRUNCATE + COPY data from public.memory_chunks/memory_relations")
        # 找源 schema (默认 public, 但用户可能用别的)
        cur.execute("""
            SELECT table_schema, table_name FROM information_schema.tables
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema', 'cortex_v7_dev')
            AND table_name IN ('memory_chunks', 'memory_relations')
        """)
        src_tables = {row['table_name']: row['table_schema'] for row in cur.fetchall()}
        print(f"  源 schema 发现: {src_tables}")
        if 'memory_chunks' not in src_tables:
            print("  ❌ 没找到 memory_chunks 源表, 退出")
            sys.exit(1)

        src_chunks = f"{src_tables['memory_chunks']}.memory_chunks"
        src_rels = f"{src_tables.get('memory_relations', src_tables['memory_chunks'])}.memory_relations" if 'memory_relations' in src_tables else None

        # 复制 memory_chunks
        cur.execute(f"TRUNCATE {SCHEMA}.memory_chunks")
        cur.execute(f"""
            INSERT INTO {SCHEMA}.memory_chunks
            (id, content, type, importance, created_at, tags, archived, tier, embedding, metadata)
            SELECT id, content, type, importance, created_at, tags, archived, tier, embedding, metadata
            FROM {src_chunks}
            WHERE archived = 0
        """)
        n_chunks = cur.rowcount
        print(f"  ✅ {SCHEMA}.memory_chunks: {n_chunks} rows")

        # 复制 memory_relations (可能不存在)
        n_rels = 0
        if src_rels:
            cur.execute(f"TRUNCATE {SCHEMA}.memory_relations")
            cur.execute(f"""
                INSERT INTO {SCHEMA}.memory_relations
                (id, memory_id_1, memory_id_2, relation_type, strength, co_occurrence_count, created_at, updated_at)
                SELECT id, memory_id_1, memory_id_2, relation_type, strength, co_occurrence_count, created_at, updated_at
                FROM {src_rels}
            """)
            n_rels = cur.rowcount
            print(f"  ✅ {SCHEMA}.memory_relations: {n_rels} rows")
        else:
            print(f"  ⚠️ 源 schema 无 memory_relations, V7 SA 评估无图可用")

        conn.commit()

        # 4. 健康检查
        print(f"[4/4] 健康检查")
        cur.execute(f"SELECT count(*) AS c FROM {SCHEMA}.memory_chunks")
        ck = cur.fetchone()['c']
        cur.execute(f"SELECT count(*) AS c FROM {SCHEMA}.memory_relations")
        rl = cur.fetchone()['c']
        print(f"  {SCHEMA}.memory_chunks: {ck}")
        print(f"  {SCHEMA}.memory_relations: {rl}")

        print()
        print(f"✅ V7 dev schema '{SCHEMA}' bootstrap 完成")
        print(f"   数据: {ck} chunks / {rl} relations")
        print(f"   连接串: postgresql://{PG_USER}:***@{PG_HOST}:{PG_PORT}/{PG_DB}?currentSchema={SCHEMA}")

    except Exception as e:
        conn.rollback()
        print(f"❌ ERROR: {e}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
