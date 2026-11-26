# Dispatch — v6.5.0 Phase 1 direction #5 HNSW m=24

**收件方**: claude-code (subagent)
**发件方**: 主 arcadia (auto-pilot 2/7)

---

## 任务

实现 #5 HNSW 参数调优 m=16 → m=24 (1 人天 spec, 小 task).

## 必做 (3 处, 见 spec.md 详情)

1. **server/docker/unified/schema.sql** — 在 hnsw index 创建后加注释 (m=24, ef_construction=200)
2. **server/src/migrations/2026_08_23_hnsw_m24.sql** — 新建 ALTER INDEX
3. **server/src/config.py** — 加 HNSW_M=24 + HNSW_EF_SEARCH=120 常量
4. **server/src/api_pg.py** — 启动时 `SET hnsw.ef_search = 120`

## 必测 (新建 server/tests/test_hnsw_v6_5.py, 3 个 test)

1. `test_schema_has_hnsw_index_comment`
2. `test_migration_sets_m24`
3. `test_config_has_hnsw_constants`

## 完成品

写 `<worktree>/.sprint/v6.5.0-phase-1-direction-5-report.md` (主 arcadia 验收报告写你这边)

## 约束

1. 只做 #5 范围 (1 人天小 task, 不扩散)
2. spec 写功能不写行数
3. 不改 Pydantic / fastapi 之外库
4. **如果 sandbox 不能跑 pytest, 直接告诉我, 主 arcadia 会跑 (Pitfall R + Pitfall P)**
5. **不 commit** (主 arcadia 自动)

## 完成后

exit 0, 主 arcadia 自动 commit + finish_task。
