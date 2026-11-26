"""ADR-072 MVP Test 1: WebUI Dashboard 加载.

验证:
- GET / 返回 200
- HTML 含 'Memory Cortex Dashboard'
- 5 个 tab 元素存在

依赖: playwright (pip install playwright && playwright install chromium)
跳过条件: 没装 playwright → pytest.skip
"""
import os
import pytest

CORTEX_URL = os.getenv("CORTEX_URL", "http://localhost:1880")


def test_dashboard_loads():
    """MVP-1: / 加载 + HTML 标题匹配."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            resp = page.goto(CORTEX_URL, timeout=10_000)
            assert resp is not None and resp.status == 200, f"status={resp.status if resp else 'none'}"
            html = page.content()
            assert "Memory Cortex Dashboard" in html, "title missing"
        finally:
            browser.close()


def test_5_tabs_present():
    """MVP-1 副: 5 个 tab 元素存在 (总览/分类/检索/图谱/API 测试)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")

    expected_tabs = ["总览", "分类", "检索", "图谱", "API"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(CORTEX_URL, timeout=10_000)
            content = page.content()
            for tab in expected_tabs:
                assert tab in content, f"tab '{tab}' missing"
        finally:
            browser.close()
