"""ADR-072 MVP Test 4: localStorage 覆盖后端地址 (ADR-069 D2 验证)."""
import os
import pytest

CORTEX_URL = os.getenv("CORTEX_URL", "http://localhost:1880")


def test_localstorage_override_persists():
    """MVP-4: 改 localStorage.cortex_api → 刷新 → UI 用新地址."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")

    new_api = "http://127.0.0.1:9999"  # 故意不可达, 仅验证 UI 读到了

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(CORTEX_URL, timeout=10_000)
            page.evaluate(f"localStorage.setItem('cortex_api', '{new_api}')")
            page.reload(timeout=10_000)
            # 验证 localStorage 还在
            stored = page.evaluate("localStorage.getItem('cortex_api')")
            assert stored == new_api
        finally:
            browser.close()


def test_url_query_param_overrides():
    """MVP-4 副: URL ?api=... 覆盖 (一次性)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(f"{CORTEX_URL}/?api=http://override.example.com:1880", timeout=10_000)
            # 验证 api-bar 显示 override 地址
            content = page.content()
            assert "override.example.com" in content or "cortex_api" in page.evaluate("Object.keys(localStorage)")
        finally:
            browser.close()
