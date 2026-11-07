"""The admin analytics chat in the UI (scripted fake LLM, real backend and DB).

The admin is the seeded demo account (seed/himali-tea/data.yaml; DEMO ONLY password), so
`make seed` must have run on the dev database.
"""

import re

from playwright.sync_api import Page, expect

from conftest import shot, sign_up

ADMIN_EMAIL = "admin@himali.test"
ADMIN_PASSWORD = "demo-admin-himali"  # DEMO ONLY, listed in the README


def log_in(page: Page, slug: str, email: str, password: str) -> None:
    page.goto(f"/a/{slug}")
    page.get_by_role("tab", name="Log in").click()
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Log in").click()
    page.wait_for_url(f"**/a/{slug}/chat")


def test_admin_sees_analytics_and_a_metric_card(page: Page) -> None:
    log_in(page, "himali-tea", ADMIN_EMAIL, ADMIN_PASSWORD)
    link = page.get_by_test_id("analytics-link")
    expect(link).to_be_visible()
    link.click()
    page.wait_for_url("**/a/himali-tea/admin")
    expect(page.get_by_text("I'm read-only")).to_be_visible()
    expect(page.get_by_role("button", name="How did we do today?")).to_be_visible()
    shot(page, "admin-empty")

    page.get_by_role("button", name="How did we do today?").click()
    card = page.get_by_test_id("metric-card").last
    expect(card).to_be_visible()
    expect(card).to_contain_text("Orders by status")
    footnote = card.get_by_test_id("metric-footnote")
    expect(footnote).to_contain_text(re.compile(r"today, \w{3} \d+ \w{3} \d{4}"))
    expect(footnote).to_contain_text("Asia/Kathmandu")
    expect(footnote).to_contain_text("counted by their current status")
    answer = page.get_by_test_id("assistant-message").last
    expect(answer).to_have_attribute("data-status", "complete")
    expect(page).to_have_url(re.compile(r"/a/himali-tea/admin/[0-9a-f-]{36}$"))
    shot(page, "admin-metric")

    # Its own conversation list, and the way back to the customer chat.
    page.reload()
    expect(page.get_by_test_id("metric-card").last).to_be_visible()
    expect(page.get_by_test_id("customer-chat-link")).to_be_visible()


def test_customer_gets_the_admins_only_screen_without_admin_requests(page: Page) -> None:
    sign_up(page, "himali-tea")
    expect(page.get_by_test_id("analytics-link")).to_have_count(0)  # no link for customers
    admin_calls: list[str] = []
    page.on("request", lambda r: admin_calls.append(r.url) if "/api/admin" in r.url else None)
    page.goto("/a/himali-tea/admin")
    expect(page.get_by_test_id("admins-only")).to_be_visible()
    expect(page.get_by_text("Analytics is for admins only")).to_be_visible()
    shot(page, "admins-only")
    page.wait_for_load_state("networkidle")
    assert admin_calls == [], admin_calls
