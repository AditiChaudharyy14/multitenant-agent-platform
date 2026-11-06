"""Shared helpers for the UI smoke tests. The backend runs LLM_PROVIDER=fake with
backend/fake_scenarios/e2e.yaml, so no LLM quota is spent and replies are predictable."""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

BASE_URL = os.environ.get("BASE_URL", "http://localhost:3000")
SHOTS = Path(os.environ.get("SCREENSHOT_DIR", "/screenshots"))
PASSWORD = "correct horse battery staple"

VIEWPORTS = {"desktop": {"width": 1280, "height": 800}, "mobile": {"width": 375, "height": 760}}


@pytest.fixture(scope="session")
def warm_routes(browser: Browser) -> None:
    """`next dev` compiles each route on its first request (tens of seconds when cold), so
    visit every route once with a long timeout before any test's own timeouts apply."""
    context = browser.new_context(base_url=BASE_URL)
    p = context.new_page()
    for path in ("/a/himali-tea", "/a/himali-tea/chat", "/a/himali-tea/chat/00000000-0000-0000-0000-000000000000"):
        p.goto(path, timeout=180_000, wait_until="networkidle")
    context.close()


@pytest.fixture(params=list(VIEWPORTS), ids=list(VIEWPORTS))
def page(request: pytest.FixtureRequest, browser: Browser, warm_routes: None) -> Iterator[Page]:
    """A fresh context (no cookies) at 1280 px and at 375 px wide."""
    context = browser.new_context(viewport=VIEWPORTS[request.param], base_url=BASE_URL)
    context.set_default_timeout(20_000)
    p = context.new_page()
    yield p
    context.close()


def shot(page: Page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}-{page.viewport_size['width']}.png"))


def sign_up(page: Page, slug: str, *, screenshot: str | None = None) -> str:
    """Sign up through the landing page's form and land in the chat. Returns the email."""
    email = f"e2e-{uuid.uuid4().hex[:10]}@example.test"
    page.goto(f"/a/{slug}")
    expect(page.get_by_role("tab", name="Sign up")).to_be_visible()
    if screenshot:
        shot(page, screenshot)
    page.get_by_role("tab", name="Sign up").click()
    page.get_by_label("Your name").fill("Asha Gurung")
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Create account").click()
    page.wait_for_url(f"**/a/{slug}/chat")
    expect(page.get_by_label("Message")).to_be_visible()
    return email


# Records, from inside the page, whether the tool status line ever appeared and how the
# assistant's text grew, so the checks don't race a fast local tool call.
WATCH_JS = """
() => {
  window.__seen = { activity: [], lengths: [] };
  new MutationObserver(() => {
    const a = document.querySelector('[data-testid=tool-activity]');
    if (a && !window.__seen.activity.includes(a.textContent)) window.__seen.activity.push(a.textContent);
    const msgs = document.querySelectorAll('[data-testid=assistant-message]');
    const last = msgs[msgs.length - 1];
    if (last) {
      const n = last.textContent.length;
      const ls = window.__seen.lengths;
      if (ls[ls.length - 1] !== n) ls.push(n);
    }
  }).observe(document.body, { subtree: true, childList: true, characterData: true });
}
"""
