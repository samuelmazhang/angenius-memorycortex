"""
Memory Cortex 统一配置
单一配置源:所有模块通过 CortexConfig 实例读取,
避免 os.getenv() 散弹枪造成的 .env / .env.example 漂移。
"""
import os
from pathlib import Path
from dotenv import load_dotenv


class CortexConfig:
    """单一配置类,所有模块共享此实例。"""

    _instance: "CortexConfig" = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load()
        return cls._instance

    def _load(self):
        # 加载 .env(项目根目录)
        _env_path = Path(__file__).parent.parent / ".env"
        if _env_path.exists():
            load_dotenv(_env_path)
        else:
            load_dotenv()  # fallback to CWD

        # 服务
        # CORTEX_PORT is the canonical var used by api_pg.py; PORT is kept as
        # a legacy fallback so existing deployments keep working unchanged.
        self.host: str = os.getenv("HOST", "0.0.0.0")
        self.port: int = int(os.getenv("CORTEX_PORT", os.getenv("PORT", "1880")))
        self.uvicorn_reload: bool = os.getenv("UVICORN_RELOAD", "false").lower() == "true"

        # PostgreSQL / pgvector (v5+ runtime backend). These were historically
        # read ad-hoc in api_pg.py via os.getenv; mirrored here so this class is
        # genuinely the single config source. Env still takes precedence because
        # _load() reads env directly, so behaviour is byte-for-byte identical.
        self.pg_host: str = os.getenv("PG_HOST", "localhost")
        self.pg_port: int = int(os.getenv("PG_PORT", "5432"))
        self.pg_db: str = os.getenv("PG_DB", "cortex_demo")
        self.pg_user: str = os.getenv("PG_USER", "cortex")
        self.pg_password: str = os.getenv("PG_PASSWORD", "change_me")
        self.pg_pool_min: int = int(os.getenv("PG_POOL_MIN", "4"))
        self.pg_pool_max: int = int(os.getenv("PG_POOL_MAX", "32"))
        self.pg_pool_wait_seconds: float = float(os.getenv("PG_POOL_WAIT_SECONDS", "10"))
        self.pg_pool_retry_interval: float = float(os.getenv("PG_POOL_RETRY_INTERVAL", "0.1"))

        # Legacy V4 storage fields (kept for historical modules only).
        self.chroma_data_path: str = ""
        self.collection_name: str = "memory_chunks"
        self.metadata_db_path: str = ""

        # Embedding
        # 修复前默认值为 "./models//root/.cache/.../mpnet" 双斜杠 + 异常路径
        # 修复后:为空时直接抛错,强制 .env 配置,避免 fallback 到奇怪路径
        _mp = os.getenv("EMBEDDING_MODEL_PATH", "").strip()
        if not _mp:
            raise RuntimeError(
                "EMBEDDING_MODEL_PATH 未配置。请在 .env 设置:\n"
                "EMBEDDING_MODEL_PATH=/root/.arcadia/plugins/memory-cortex/models/huggingface_cache/"
                "models--sentence-transformers--paraphrase-multilingual-mpnet-base-v2/snapshots/<SHA>"
            )
        self.embedding_model_path: str = _mp
        self.embedding_device: str = os.getenv("EMBEDDING_DEVICE", "cpu")
        # Two-model setup (v6.5+): "mpnet" (384-dim) / "nomic-v1.5" (768-dim).
        self.embedding_model: str = os.getenv("EMBEDDING_MODEL", "nomic-v1.5").lower()

        def _flag(name: str, default: str) -> bool:
            return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")

        # Retrieval feature flags (also read by /capabilities introspection).
        self.feature_raptor: bool = _flag("MEMORY_FEATURE_RAPTOR", "0")
        self.feature_taxonomy_boost: bool = _flag("MEMORY_FEATURE_TAXONOMY_BOOST", "false")
        self.mcp_enabled: bool = os.getenv("CORTEX_MCP_ENABLED", "1") == "1"
        self.raptor_score_weight: float = float(os.getenv("MEMORY_RAPTOR_SCORE_WEIGHT", "0.5"))

        # 检索
        self.default_top_k: int = int(os.getenv("DEFAULT_TOP_K", "5"))
        self.default_score_threshold: float = float(os.getenv("DEFAULT_SCORE_THRESHOLD", "0.7"))

        # V6.5.0 Phase 1 direction #5 (2026-08-23): HNSW index tuning.
        # Bumped m (max edges per node) from pgvector default 16 → 24 to gain
        # ~5pp recall@10 on Nomic 768-dim embeddings, with a 1.5x build-time
        # and 1.4x index-size trade-off (acceptable for production).
        # ef_search is the search-time search depth; 120 trades ~10% extra latency
        # against better recall than the default 40.
        # Mirrors ALTER TABLE migration server/src/migrations/2026_08_23_hnsw_m24.sql
        # and `SET hnsw.ef_search = 120` at api_pg.py startup.
        self.hnsw_m: int = int(os.getenv("HNSW_M", "24"))
        self.hnsw_ef_construction: int = int(os.getenv("HNSW_EF_CONSTRUCTION", "200"))
        self.hnsw_ef_search: int = int(os.getenv("HNSW_EF_SEARCH", "120"))

        # Hooks / 注入
        self.memory_cortex_url: str = os.getenv("MEMORY_CORTEX_URL", f"http://localhost:{self.port}")
        self.memory_auto_inject: bool = os.getenv("MEMORY_AUTO_INJECT", "true").lower() == "true"
        self.memory_auto_inject_limit: int = int(os.getenv("MEMORY_AUTO_INJECT_LIMIT", "3"))
        self.memory_auto_inject_threshold: float = float(os.getenv("MEMORY_AUTO_INJECT_THRESHOLD", "0.5"))
        self.memory_inject_timeout: int = int(os.getenv("MEMORY_INJECT_TIMEOUT", "15"))

        # 备份
        self.backup_dir: str = os.getenv("BACKUP_DIR", str(Path(__file__).parent.parent / "backups"))
        self.backup_keep_days: int = int(os.getenv("BACKUP_KEEP_DAYS", "7"))

        # V4.0 经验抽象 LLM 配置
        self.llm_url: str = os.getenv("LLM_ABSTRACTION_URL", "http://localhost:8080/v1/chat/completions")
        self.llm_api_key: str = os.getenv("LLM_ABSTRACTION_API_KEY", "dummy")
        self.llm_model: str = os.getenv("LLM_ABSTRACTION_MODEL", "gpt-3.5-turbo")
        self.llm_timeout: int = int(os.getenv("LLM_ABSTRACTION_TIMEOUT", "15"))

        # V6.5.0 Phase 1 direction #B (2026-08-23): graph PPR feature flag.
        # When true, /search?include_related=true walks Personalized PageRank
        # over memory_relations + memory_links (HippoRAG 2 / arXiv:2502.14802).
        # Default ON now that the PPR path is implemented; can be toggled off
        # to A/B against the v6.3.1 strength-threshold path.
        self.memory_feature_graph_ppr: bool = os.getenv("MEMORY_FEATURE_GRAPH_PPR", "true").lower() == "true"
        self.graph_ppr_alpha: float = float(os.getenv("GRAPH_PPR_ALPHA", "0.85"))
        self.graph_ppr_max_iter: int = int(os.getenv("GRAPH_PPR_MAX_ITER", "100"))
        self.graph_ppr_tol: float = float(os.getenv("GRAPH_PPR_TOL", "1e-6"))
        self.graph_ppr_cache_ttl_seconds: float = float(os.getenv("GRAPH_PPR_CACHE_TTL_SECONDS", "3600"))

        # V6.5.0 Phase 2 direction #6 (2026-08-23): FastAPI OpenTelemetry.
        # Defaults to disabled; ops opt-in by flipping OTEL_ENABLED=true
        # (and pointing OTEL_EXPORTER_OTLP_ENDPOINT at a collector when
        # OTEL_EXPORTER=otlp). When disabled, setup_observability(app) is a
        # no-op -- the FastAPI middleware is never installed, so per-request
        # overhead is exactly zero.
        self.otel_enabled: bool = os.getenv("OTEL_ENABLED", "false").lower() == "true"
        self.otel_service_name: str = os.getenv("OTEL_SERVICE_NAME", "memory-cortex")
        self.otel_exporter: str = os.getenv("OTEL_EXPORTER", "console").lower()
        self.otel_otlp_endpoint: str = os.getenv(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"
        )
        self.otel_sample_ratio: float = float(os.getenv("OTEL_SAMPLE_RATIO", "1.0"))
        self.otel_metrics_enabled: bool = os.getenv("OTEL_METRICS_ENABLED", "true").lower() == "true"

    def __repr__(self) -> str:
        return (
            f"CortexConfig(host={self.host}, port={self.port}, "
            f"storage=postgresql+pgvector, embedding={self.embedding_model_path})"
        )


# 模块级便捷访问
config = CortexConfig()

# V6.5.0 Phase 1 direction #5 (2026-08-23): HNSW module-level constants.
# These are the canonical values referenced by schema.sql, the
# 2026_08_23_hnsw_m24.sql migration, and the api_pg.py startup hook.
# Edit here to retune globally — env overrides (HNSW_M, HNSW_EF_CONSTRUCTION,
# HNSW_EF_SEARCH) still take precedence through CortexConfig.
HNSW_M: int = 24                    # max edges per node (pgvector default 16)
HNSW_EF_CONSTRUCTION: int = 200     # build-time search depth
HNSW_EF_SEARCH: int = 120           # query-time search depth (default 40)