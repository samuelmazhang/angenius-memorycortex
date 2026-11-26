# ADR-072: WebUI E2E 测试 (Playwright) (2026-08-06)

- Status: Accepted
- Date: 2026-08-06
- Author: Hermes Agent (MiniMax-M3)
- Depends on: ADR-069 (WebUI 修复 + API 自动探测)
- Supersedes: 无

## Context

ADR-069 §D5 列了"已知遗留":
> trigger 测试: 仅测 1 次 INSERT, 未在 CI 跑回归。
> webui E2E 测试 (Playwright, 跟历史 SA5 e2e 经验)

WebUI 修复后, 无自动化回归测试覆盖。手动验证流程:
1. 启动容器 → curl /health
2. 浏览器打开 `http://<host>:1880/` → 看 dashboard
3. 点 5 个 tab → 各 tab 不报 500
4. 改 `localStorage.cortex_api` → 刷新 → 后端地址更新

**问题**: 手动验证易漏, 改 WebUI 没人知道有没有破。

## Decision

**Playwright** (Python) + **headless Chromium** + 端到端覆盖 5 个 tab。

### 测试范围 (MVP)

| 测试 | 验证 |
|------|------|
| `test_dashboard_loads.py` | `/` 返回 200 + HTML 含 "Memory Cortex Dashboard" |
| `test_5_tabs_navigate.py` | 5 个 tab (总览/分类/检索/图谱/API 测试) 都能点开, 无 console error |
| `test_api_tester_sends.py` | API 测试 tab 选一个 endpoint, 点"测试", 拿到响应 |
| `test_localstorage_override.py` | 改 `localStorage.cortex_api`, 刷新, UI 显示新地址 |

**不在 MVP 范围**:
- 跨浏览器 (只 Chromium)
- 视觉回归 (像素 diff)
- 真实用户操作 (登录/登出)
- 性能压测

### 实现

```python
# server/tests/e2e/test_webui_dashboard.py
from playwright.sync_api import sync_playwright

CORTEX_URL = "http://localhost:1880"

def test_dashboard_loads():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        resp = page.goto(CORTEX_URL)
        assert resp.status == 200
        assert "Memory Cortex Dashboard" in page.content()
        browser.close()
```

### CI 集成

`.gitlab-ci.yml` 加 stage:
```yaml
e2e:
  stage: test
  image: mcr.microsoft.com/playwright/python:v1.45.0-jammy
  services: [cortex:latest]
  script:
    - pip install -r requirements.txt
    - pytest server/tests/e2e/ -v --tb=short
  only: [main, develop]
```

## Consequences

### 正面
- WebUI 改完自动跑回归, 不再"改 UI 等用户反馈才发现坏了"
- 覆盖 5 个 tab 端到端, 比单元测试更接近真实使用
- 复用 ADR-069 修复的 API 自动探测 (localStorage 覆盖 test)

### 负面 / 风险
- Playwright 镜像 1.5GB, CI 时间 +30s
- headless Chromium 在某些 Linux 缺字体, 需装 `fonts-noto-cjk`
- 真实部署的 URL 不一定是 `localhost:1880`, 测试要用 env 注入

## Verification

- [ ] MVP 4 个 test 跑通 (本地 + docker)
- [ ] CI 集成 (下期)
- [ ] WebUI 改 1 处后跑 e2e 应 red, 改完后应 green (闭环验证)

## Out of Scope

- 跨浏览器 (Firefox/Safari) — Playwright 支持, 但 MVP 不写
- 视觉回归 (像素 diff) — 需额外 baseline 维护, ROI 低
- 性能 E2E — 需 k6/locust, 独立工具链
