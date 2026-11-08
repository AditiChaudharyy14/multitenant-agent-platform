"""Browser-side security: headers on every page, and a reply full of HTML renders as text."""

from playwright.sync_api import Page, expect

from conftest import sign_up


def test_pages_send_security_headers(page: Page) -> None:
    response = page.goto("/a/himali-tea")
    assert response is not None
    headers = response.headers
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    csp = headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp and "connect-src 'self'" in csp


def test_html_in_a_reply_renders_as_text_and_links_are_safe(page: Page) -> None:
    sign_up(page, "himali-tea")
    page.get_by_label("Message").fill("Show me some HTML")
    page.get_by_label("Message").press("Enter")
    answer = page.get_by_test_id("assistant-message").last
    expect(answer).to_have_attribute("data-status", "complete")
    assert page.evaluate("window.__xss") is None  # neither the <script> nor onerror ran
    expect(answer.locator("img")).to_have_count(0)
    expect(answer.locator("script")).to_have_count(0)
    link = answer.get_by_role("link", name="a link")
    expect(link).to_have_attribute("rel", "noopener noreferrer")
    expect(link).to_have_attribute("target", "_blank")
