"""Memory taxonomy boost (ADR-067 M8).

Implements ADR-067 §3.8 M8 — taxonomy 父子层级加权.

设计: chunk.tag 命中 tags_taxonomy 任何祖先层级 → composite_score × 1.2

数据表: tags_taxonomy(tag, parent_tag, description, created_at)
    -- 见 server/docker/unified/schema.sql

Feature flag: MEMORY_FEATURE_TAXONOMY_BOOST (默认 false)
计数: 由 api_pg.OPS_COUNTERS["taxonomy_boost_applied"] 跟踪

Usage:
    from .memory_taxonomy import apply_taxonomy_boost

    if apply_taxonomy_boost(chunk_tags, query_tags) > 1.0:
        composite *= 1.2

模块设计: 独立文件不依赖 psycopg2 → 测试可用 mock DB 而不必装 psycopg2.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Feature flag (灰度)
TAXONOMY_BOOST_ENABLED = os.getenv("MEMORY_FEATURE_TAXONOMY_BOOST", "false").lower() in ("1", "true", "yes")

# 加权系数 (按设计.md §9 + ADR-067 §3.8)
TAXONOMY_BOOST_FACTOR = 1.2

# 祖先缓存: tag -> [self, parent, grandparent, ...]
# LRU 策略 (生产用 LRU 装饰器, 这里简化: 进程内 dict, 重启清空)
_ANCESTOR_CACHE: Dict[str, List[str]] = {}


def reset_cache() -> None:
    """清空祖先缓存. 测试/管理用."""
    global _ANCESTOR_CACHE
    _ANCESTOR_CACHE = {}


def get_ancestors(
    tag: str,
    db_lookup: Optional[Any] = None,
    max_depth: int = 10,
) -> List[str]:
    """递归获取 tag 的所有祖先 (含自身).

    Args:
        tag: 起始 tag
        db_lookup: 可调用的 DB 查询函数. 接受 (sql, params, fetch) -> row 或 None.
                    默认 None 时用 None 注入 (测试场景).
                    生产注入: `from .api_pg import query` 或外部 DB client.
        max_depth: 防环 + 防超深链

    Returns:
        [tag, parent1, parent2, ...] (从 tag 自身到根)

    Raises:
        ValueError: 环检测到时警告 + 截断

    Note:
        DB 失败 → 返回 [tag] 安全降级, 不会抛错 (避免污染主路径).
    """
    if tag in _ANCESTOR_CACHE:
        return _ANCESTOR_CACHE[tag]

    if db_lookup is None:
        # 无 DB 可用: 返回仅自身 (避免测试误用)
        logger.debug(f"taxonomy.get_ancestors({tag}) no db_lookup, returning [tag]")
        return [tag]

    try:
        ancestors = [tag]
        current = tag
        for _ in range(max_depth):
            row = db_lookup(
                "SELECT parent_tag FROM tags_taxonomy WHERE tag = %s",
                (current,),
                fetch="one",
            )
            if not row or not row.get("parent_tag"):
                break
            parent = row["parent_tag"]
            if parent in ancestors:  # 环检测
                logger.warning(f"Taxonomy cycle detected: {ancestors} -> {parent}")
                break
            ancestors.append(parent)
            current = parent
    except Exception as e:
        logger.warning(f"Taxonomy ancestor lookup failed for {tag}: {e}")
        ancestors = [tag]  # 降级

    _ANCESTOR_CACHE[tag] = ancestors
    return ancestors


def boost_factor(
    chunk_tags: Optional[List[str]],
    query_tags: Optional[List[str]],
    db_lookup: Optional[Any] = None,
) -> float:
    """计算 taxonomy 加权系数.

    Args:
        chunk_tags: chunk.tags (list[str] or None)
        query_tags: req.tags (list[str] or None)
        db_lookup: 可选 DB 查询函数 (see get_ancestors)

    Returns:
        1.0 (无 boost) 或 1.2 (命中)

    逻辑: chunk_tags 任一 tag 的祖先集合 ∩ query_tags 非空 → 1.2
    """
    if not TAXONOMY_BOOST_ENABLED:
        return 1.0
    if not chunk_tags or not query_tags:
        return 1.0
    qset = set(query_tags if isinstance(query_tags, list) else [query_tags])
    for t in chunk_tags:
        ancestors = get_ancestors(t, db_lookup=db_lookup)
        if any(a in qset for a in ancestors):
            return TAXONOMY_BOOST_FACTOR
    return 1.0


# 模块级便捷函数: 接受真实 DB client (api_pg.query)
def is_enabled() -> bool:
    """检查 feature flag."""
    return TAXONOMY_BOOST_ENABLED


def factor() -> float:
    """获取加权系数常量."""
    return TAXONOMY_BOOST_FACTOR
