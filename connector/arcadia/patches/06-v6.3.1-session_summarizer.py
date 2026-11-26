"""Session auto-summarizer: 9b 摘要 + cortex (sole source of truth) + 本地 fast cache.

V6.3.1 (2026-08-18) 设计要点:
1. 触发时机:
   - session end (end_session 末尾 fire-and-forget)
   - 渐进式 (每 PROGRESSIVE_THRESHOLD 条 assistant 消息追加一次,避免 session 崩溃漏摘要)
2. 双轨写入 - **cortex 是 sole source of truth**:
   - 先写 cortex (upsert_session_conflict → 返回 chunk_id)
   - 成功后才写本地 fast cache (upsert_session_summary + 回填 cortex_chunk_id)
   - cortex 失败 → 本地 cache 不写, 调用方看到 cortex_id=None (no fake-success)
   - 本地失败 → cortex 已经写了, 调用方不感知 (cache miss 是允许的)
3. 读取策略: 优先本地 cache (fast), cache miss 时回退 cortex `/list?source=arcadia-session-summarizer`
4. 9b 跑摘要 (qwen3.5-9b @ q5_k_xl, LM Studio localhost:8080):
   - thinking=off 强制 (9b 默认 ON 锁 max_tokens)
   - max_tokens=400 限死
5. cortex 调用复用 connector/arcadia/cortex_memory_backend._request (已有
   retry + circuit breaker) 而非裸 requests,跟 memory_tool 走同一通道.

公开 API:
- summarize_session(session_id, db: SessionDB) -> dict  # 同步阻塞版
- summarize_session_async(session_id, db: SessionDB)  # fire-and-forget
- get_prior_summaries(user_id, db: SessionDB, limit=5) -> list
- format_prior_summaries_for_prompt(summaries) -> str

部署: 此文件 source of truth 在 .
connector\\arcadia\\session_summarizer.py。Arcadia 端同步靠 scripts/install_*.py。

Arcadia 内部 patch 依赖 (V6.3.1 首次正式落地, 见 connector/arcadia/ARCADIA_INTERNAL_PATCHES.md):
- arcadia_state_common.py: session_summaries 表 schema
- arcadia_state.py SessionDB.upsert_session_summary / update_session_summary_cortex_id / list_session_summaries
- arcadia_state.py end_session + append_message 钩子
- agent/conversation_loop.py 首轮 prior-summary 注入
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

import requests  # 9b 直接打 LM Studio (本地 endpoint,不走 cortex client)

# connector 提供 retry + circuit breaker 的 cortex client
try:
    # V6.3.1: package-style import (mirrors tools/memory_tool.py) so it works
    # both when tools/ is on sys.path AND when it's a proper subpackage
    # (pytest --import-mode=importlib). The previous bare ``from cortex_memory_backend``
    # form only worked in the first case and silently fell back to the
    # ImportError branch in tests.
    from tools.cortex_memory_backend import (
        upsert_session_conflict,
        CortexUnavailableError,
    )
    _HAS_CONNECTOR = True
except ImportError as e:
    # connector 不可用时 (例如裸单测环境),降级为直接 HTTP,但记录警告.
    # V6.3.1 FIX: bind module-level sentinels so summarize_session can still
    # reference them without NameError — earlier versions crashed with
    # "name 'upsert_session_conflict' is not defined" the moment any caller
    # ran in a stripped env, which then masked the real cortex import error.
    upsert_session_conflict = None  # type: ignore[assignment]
    CortexUnavailableError = type(  # type: ignore[assignment,misc]
        "CortexUnavailableError", (Exception,), {}
    )
    _HAS_CONNECTOR = False
    _IMPORT_ERROR = str(e)

logger = logging.getLogger(__name__)

# 9b LM Studio endpoint (本地,不走 cortex client)
LM_STUDIO_URL = os.getenv("LM_STUDIO_URL", "http://localhost:8080/v1")
LM_STUDIO_MODEL = os.getenv("LM_STUDIO_MODEL", "qwen3.5-9b")  # thinking 默认 ON
LM_STUDIO_TIMEOUT = float(os.getenv("LM_STUDIO_TIMEOUT", "15"))

# cortex
CORTEX_URL = os.getenv("MEMORY_CORTEX_URL", "http://127.0.0.1:1880")
CORTEX_TIMEOUT = float(os.getenv("MEMORY_CORTEX_TIMEOUT", "10"))
SUMMARY_IMPORTANCE = int(os.getenv("SESSION_SUMMARY_IMPORTANCE", "7"))  # long_term, 180 天

# 渐进式触发阈值 (assistant 消息数)
PROGRESSIVE_THRESHOLD = int(os.getenv("SESSION_SUMMARY_PROGRESSIVE", "50"))

SUMMARY_SYSTEM_PROMPT = """你是会话摘要器。从一段对话历史里提取 3-5 条**对未来对话有长期价值**的事实/偏好/任务状态。

输出格式 (strict JSON, 不要 markdown):
{"facts": ["事实1 ≤30字", "事实2 ≤30字", ...], "user_preferences": ["偏好1 ≤20字"], "open_tasks": ["未完成任务 ≤30字"]}

规则:
- 优先保留:用户偏好/项目状态/工具链决策/反复出现的主题
- 丢弃:问候语/重复确认/调试细节/单次性命令
- 用户偏好 = 关于用户本人的习惯/工作方式/项目背景
- open_tasks = 提到但未完成的工作
- facts 数 3-5 条,preferences 0-3 条,open_tasks 0-3 条,总 ≤ 200 字
- 只返 JSON,不要解释"""


def _call_9b(messages: List[Dict[str, str]]) -> str:
    """调 LM Studio 9b 跑摘要. thinking 强制 off."""
    r = requests.post(
        f"{LM_STUDIO_URL}/chat/completions",
        json={
            "model": LM_STUDIO_MODEL,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 400,
            # 9b thinking 默认 ON 锁 max_tokens;chat_template kwargs 关掉
            "chat_template_kwargs": {"enable_thinking": False},
        },
        timeout=LM_STUDIO_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"].strip()


def _parse_summary_json(raw: str) -> Dict[str, List[str]]:
    """解析 9b 输出. 兼容 dict-literal 风格 + 严格 JSON."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 9b 有时输出 {facts: [...]} (Python literal) — 转 JSON
        text2 = re.sub(r"(\w+):", r'"\1":', text)
        text2 = re.sub(r"'", '"', text2)
        try:
            return json.loads(text2)
        except json.JSONDecodeError:
            logger.warning("Failed to parse 9b summary JSON, raw=%r", raw[:200])
            return {"facts": [raw[:200]], "user_preferences": [], "open_tasks": []}


def _format_summary(parsed: Dict[str, List[str]], session_id: str, source: str) -> str:
    """把 9b 解析后的 dict 转成 cortex chunk content."""
    lines = [f"[session:{session_id} source:{source}]"]
    if parsed.get("facts"):
        lines.append("Facts:")
        for f in parsed["facts"][:5]:
            lines.append(f"- {f}")
    if parsed.get("user_preferences"):
        lines.append("Preferences:")
        for p in parsed["user_preferences"][:3]:
            lines.append(f"- {p}")
    if parsed.get("open_tasks"):
        lines.append("OpenTasks:")
        for t in parsed["open_tasks"][:3]:
            lines.append(f"- {t}")
    return "\n".join(lines)


def _format_for_system_prompt(parsed: Dict[str, List[str]]) -> str:
    """短摘要,塞下个 session 的 system prompt (限 500 字)."""
    lines = []
    if parsed.get("facts"):
        lines.append("上次会话要点:")
        for f in parsed["facts"][:5]:
            lines.append(f"- {f}")
    if parsed.get("user_preferences"):
        lines.append("用户偏好:")
        for p in parsed["user_preferences"][:3]:
            lines.append(f"- {p}")
    if parsed.get("open_tasks"):
        lines.append("未完成任务:")
        for t in parsed["open_tasks"][:3]:
            lines.append(f"- {t}")
    return "\n".join(lines)[:500]


def _load_session_messages(db, session_id: str, max_chars: int = 30000) -> List[Dict[str, Any]]:
    """拉 session 所有 active 消息,过滤 tool 噪声. 超 max_chars 截断.

    优先用 SessionDB.get_messages() (production),fallback 到 SQLite 直连
    (用于单元测试里的 MockDB).
    """
    rows = None
    if hasattr(db, "get_messages"):
        try:
            rows = db.get_messages(session_id)  # include_inactive=False by default
        except Exception as e:
            logger.debug("[summary] get_messages failed, falling back to raw query: %s", e)
    if rows is None:
        # fallback: raw SQLite via the SessionDB.db_path attribute
        import sqlite3 as _sqlite3
        db_path = getattr(db, "db_path", None)
        if db_path is None:
            logger.warning("[summary] no db_path on db, returning empty messages")
            return []
        conn = _sqlite3.connect(str(db_path))
        try:
            raw = conn.execute(
                "SELECT role, content, tool_name FROM messages "
                "WHERE session_id = ? AND active = 1 ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            rows = [{"role": r[0], "content": r[1] or "", "tool_name": r[2]} for r in raw]
        finally:
            conn.close()

    out = []
    total = 0
    for r in rows:
        role = r.get("role", "?")
        content = r.get("content") or ""
        tool_name = r.get("tool_name")
        if role == "tool" and len(content) > 1000:
            content = content[:300] + "... [tool output truncated]"
        if role == "assistant" and not content.strip():
            continue
        line = f"[{role}{':' + tool_name if tool_name else ''}] {content}"
        if total + len(line) > max_chars:
            out.append(f"[truncated at {total} chars]")
            break
        out.append(line)
        total += len(line)
    return out


def _resolve_user_id(db, session_id: str) -> str:
    """Resolve user_id for a session via SessionDB.get_session().

    Returns "" if user_id is missing/None (e.g. CLI sessions without
    gateway-attributed user_id). Callers should treat "" as "no user
    attribution" — useful for anonymous recall but not for per-user filtering.
    """
    if hasattr(db, "get_session"):
        try:
            sess = db.get_session(session_id)
            if isinstance(sess, dict):
                uid = sess.get("user_id")
                if uid:
                    return str(uid)
        except Exception as e:
            logger.debug("[summary] get_session failed for user_id lookup: %s", e)
    return ""


def summarize_session(db, session_id: str, source: str = "end") -> Dict[str, Any]:
    """同步跑摘要 + 推 cortex + 写本地 fast cache. 返回 dict 包含 parsed + cortex_id.

V6.3.1 双轨写入顺序:
  1. 跑 9b 摘要
  2. 推 cortex (upsert_session_conflict, sole source of truth)
  3. 只有 cortex 成功才写本地 cache (upsert_session_summary)
  4. 回填 cortex_chunk_id 到本地 row
"""
    try:
        msgs = _load_session_messages(db, session_id)
        if len(msgs) < 3:
            logger.info("[summary] session %s too short (%d msgs), skip", session_id, len(msgs))
            return {"skipped": True, "reason": "too_short"}

        history = "\n".join(msgs[-60:])  # 最近 60 条
        messages = [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": history},
        ]
        raw = _call_9b(messages)
        parsed = _parse_summary_json(raw)
        summary_text = _format_summary(parsed, session_id, source)
        short = _format_for_system_prompt(parsed)

        # 解析 user_id 给 cortex (保留给 source_key 命名空间,
        # 防止不同用户的同名 session 摘要互相冲突; 没有 user_id 时 fallback 'anon')
        user_id = _resolve_user_id(db, session_id)

        # V6.3.1: cortex 先 — sole source of truth. cortex 失败 → 本地不写.
        # 走 connector 的 upsert_session_conflict (retry + circuit breaker +
        # server-side conflict-resolve: 同 source_key 第 2+ 次写入 SUPERSEDE 旧 chunk).
        # source_key 桶: per (user_id, session_id) — 同一 session 多次摘要自动 supersede.
        cortex_id = None
        cortex_decision = None
        cortex_error = None
        source_key = f"session_summary:{user_id or 'anon'}:{session_id}"
        if not _HAS_CONNECTOR or upsert_session_conflict is None:
            # connector 模块不可用 (单测环境, broken install). 写 cortex 的入口没了,
            # cortex 是 sole source of truth — 直接放弃本地写入, 跟 cortex 失败同语义.
            logger.warning(
                "[summary] cortex connector unavailable (%s); "
                "skipping local cache per V6.3.1 source-of-truth policy",
                _IMPORT_ERROR,
            )
            return {
                "skipped": False,
                "parsed": parsed,
                "summary_text": summary_text,
                "short": short,
                "local_id": None,
                "cortex_id": None,
                "cortex_decision": None,
                "cortex_error": "connector_unavailable",
            }
        try:
            response = upsert_session_conflict(
                content=summary_text,
                source_key=source_key,
                importance=SUMMARY_IMPORTANCE,
                session_id=session_id,
                trigger=source,
            )
            cortex_id = response.get("chunk_id")
            cortex_decision = response.get("decision", {})
        except CortexUnavailableError as e:
            cortex_error = str(e)[:200]
            logger.warning(
                "[summary] cortex unavailable — local cache NOT written "
                "(cortex is sole source of truth): %s", e,
            )
            # 立即返,不再写本地
            return {
                "skipped": False,
                "parsed": parsed,
                "summary_text": summary_text,
                "short": short,
                "local_id": None,
                "cortex_id": None,
                "cortex_decision": None,
                "cortex_error": cortex_error,
            }
        except Exception as e:
            cortex_error = str(e)[:200]
            logger.warning(
                "[summary] cortex push failed — local cache NOT written: %s", e,
            )
            return {
                "skipped": False,
                "parsed": parsed,
                "summary_text": summary_text,
                "short": short,
                "local_id": None,
                "cortex_id": None,
                "cortex_decision": None,
                "cortex_error": cortex_error,
            }

        # cortex 成功 → 写本地 fast cache
        local_id = None
        try:
            local_id = db.upsert_session_summary(session_id, summary_text, short, parsed)
        except Exception as e:
            logger.warning(
                "[summary] local cache write failed (cortex already has chunk %s): %s",
                cortex_id, e,
            )
            # 不返错: cortex 已经写了, 本地 cache miss 是允许的

        # 回填 cortex_chunk_id 到本地 row (如果有)
        if cortex_id and local_id:
            try:
                db.update_session_summary_cortex_id(local_id, cortex_id)
            except Exception as e:
                logger.debug("[summary] cortex_id backfill failed: %s", e)

        return {
            "skipped": False,
            "parsed": parsed,
            "summary_text": summary_text,
            "short": short,
            "local_id": local_id,
            "cortex_id": cortex_id,
            "cortex_decision": cortex_decision,
            "cortex_error": cortex_error,
        }
    except Exception as e:
        logger.error("[summary] unexpected failure for %s: %s", session_id, e, exc_info=True)
        return {"skipped": True, "reason": "exception", "error": str(e)[:200]}


def summarize_session_async(db, session_id: str, source: str = "end") -> None:
    """后台线程 fire-and-forget. 不阻塞 caller."""
    t = threading.Thread(
        target=summarize_session,
        args=(db, session_id, source),
        daemon=True,
        name=f"summary-{session_id[:16]}",
    )
    t.start()


def get_prior_summaries(
    db,
    user_id: str,
    limit: int = 5,
    *,
    fallback_to_cortex: bool = True,
) -> List[Dict[str, Any]]:
    """下个 session 启动时拉最近 N 条用户摘要. 返回 list of dict.

    V6.3.1 读取策略:
      1. 优先本地 cache (fast index, O(N) over 5 rows)
      2. cache miss 或 < limit 时回退 cortex `/list?source=arcadia-session-summarizer`
         (cortex is sole source of truth, 本地只是 mirror)
      3. cortex 也 fail → 返空, 调用方降级到无 prior context
    """
    if not user_id:
        return []
    out: List[Dict[str, Any]] = []
    # 1) 本地 cache
    try:
        rows = db.list_session_summaries(user_id=user_id, limit=limit) or []
        out.extend(rows)
    except Exception as e:
        logger.warning("[summary] get_prior_summaries local cache failed: %s", e)
    # 2) cortex fallback (V6.3.1)
    if fallback_to_cortex and len(out) < limit:
        try:
            # V6.3.1: package-style import (same fix as top-of-file)
            from tools.cortex_memory_backend import list_session_summaries_for_user
            cortex_rows = list_session_summaries_for_user(user_id=user_id, limit=limit)
            # 去重: 按 chunk_id / session_id+source
            seen = {(r.get("session_id"), r.get("source")) for r in out}
            for r in cortex_rows:
                key = (r.get("session_id"), r.get("source"))
                if key not in seen:
                    out.append(r)
                    seen.add(key)
        except ImportError:
            pass  # 模块不可用, 静默
        except Exception as e:
            logger.debug("[summary] cortex fallback failed (non-fatal): %s", e)
    return out[:limit]


def format_prior_summaries_for_prompt(summaries: List[Dict[str, Any]]) -> str:
    """把 list 转成 system prompt 段落."""
    if not summaries:
        return ""
    lines = ["## 历史会话背景 (auto-extracted from prior sessions)\n"]
    for i, s in enumerate(summaries, 1):
        sid = s.get("session_id", "?")[:25]
        when = time.strftime("%Y-%m-%d", time.localtime(s.get("created_at", 0)))
        short = s.get("short") or s.get("summary_text", "")[:300]
        lines.append(f"### [{i}] session {sid} ({when})\n{short}\n")
    return "\n".join(lines)
