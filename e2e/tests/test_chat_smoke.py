"""UI smoke tests against the running stack (scripted fake LLM, real backend and DB)."""

import re
from urllib.parse import urlparse

from playwright.sync_api import Page, expect

from conftest import WATCH_JS, shot, sign_up

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def test_sign_up_ask_and_open_the_cited_source(page: Page) -> None:
    sign_up(page, "himali-tea", screenshot="landing")
    expect(page.get_by_text("Namaste! I'm Chiya")).to_be_visible()  # empty state: greeting
    expect(page.get_by_role("button", name="How long does delivery take?")).to_be_visible()
    shot(page, "chat-empty")

    page.evaluate(WATCH_JS)
    page.get_by_label("Message").fill("How long does delivery take?")
    page.get_by_label("Message").press("Enter")

    chip = page.get_by_test_id("citation-chip").first
    expect(chip).to_be_visible()
    answer = page.get_by_test_id("assistant-message").last
    expect(answer).to_have_attribute("data-status", "complete")
    expect(answer).to_contain_text("Kathmandu Valley")
    expect(page).to_have_url(re.compile(rf"/a/himali-tea/chat/{UUID}$"))  # the new conversation's URL

    seen = page.evaluate("window.__seen")
    assert any("help pages" in a or "source" in a for a in seen["activity"]), seen["activity"]
    assert len(seen["lengths"]) >= 3, f"answer didn't stream in pieces: {seen['lengths']}"
    shot(page, "chat-answer")

    chip.click()
    drawer = page.get_by_test_id("source-drawer")
    expect(drawer).to_be_visible()
    expect(drawer).to_contain_text("Frequently Asked Questions")
    expect(drawer).to_contain_text(re.compile("deliver", re.I))
    shot(page, "source-drawer")
    page.keyboard.press("Escape")
    expect(drawer).to_be_hidden()

    # History reloads with the stored citation re-rendered.
    page.reload()
    expect(page.get_by_test_id("citation-chip").first).to_be_visible()


def test_himali_account_opening_lotus_chat_sees_the_belongs_to_screen(page: Page) -> None:
    email = sign_up(page, "himali-tea")
    api_calls: list[str] = []
    page.on("request", lambda r: api_calls.append(urlparse(r.url).path) if "/api/" in r.url else None)

    page.goto("/a/lotus-wellness/chat")
    screen = page.get_by_test_id("wrong-org")
    expect(screen.get_by_role("heading", name="This account belongs to Himali Tea House")).to_be_visible()
    expect(screen).to_contain_text(email)
    expect(screen.get_by_role("link", name="Go to Himali Tea House")).to_have_attribute("href", "/a/himali-tea/chat")
    page.wait_for_load_state("networkidle")
    shot(page, "wrong-org")

    # Only the session's own identity was asked for: nothing for the other org. (React's
    # dev StrictMode runs effects twice, so /api/me may appear twice.)
    assert api_calls and set(api_calls) == {"/api/me"}, api_calls

    screen.get_by_role("link", name="Go to Himali Tea House").click()
    page.wait_for_url("**/a/himali-tea/chat")
    expect(page.get_by_label("Message")).to_be_visible()


def test_stop_mid_stream_marks_the_reply_stopped(page: Page) -> None:
    sign_up(page, "himali-tea")
    box = page.get_by_label("Message")
    box.fill("tell me")
    box.press("Shift+Enter")  # a new line, not a send
    box.press_sequentially("a long story")
    expect(box).to_have_value("tell me\na long story")
    box.press("Enter")

    answer = page.get_by_test_id("assistant-message").last
    expect(answer).to_contain_text("Once upon a time")
    page.get_by_test_id("stop").click()

    expect(answer.get_by_test_id("stopped-tag")).to_be_visible()
    expect(answer).to_have_attribute("data-status", "stopped")
    partial = answer.inner_text()
    assert "People still talk about it" not in partial  # cut off before the end
    page.wait_for_timeout(600)
    assert answer.inner_text() == partial  # nothing more arrives after Stop

    # The server kept the partial reply as stopped too.
    expect(page).to_have_url(re.compile(rf"/chat/{UUID}$"))
    page.wait_for_timeout(500)  # let the cancelled turn persist
    page.reload()
    stored = page.get_by_test_id("assistant-message").last
    expect(stored).to_have_attribute("data-status", "stopped")
    expect(stored.get_by_test_id("stopped-tag")).to_be_visible()
