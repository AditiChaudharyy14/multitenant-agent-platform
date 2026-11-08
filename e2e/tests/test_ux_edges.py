"""UX edges: an unknown business slug, and a session that ends mid-chat."""

import pytest
from playwright.sync_api import Page, expect

from conftest import sign_up


@pytest.mark.allow_http_errors(404)
def test_an_unknown_slug_shows_the_not_found_page_with_a_404(page: Page) -> None:
    response = page.goto("/a/no-such-business")
    assert response is not None and response.status == 404
    expect(page.get_by_role("heading", name="We couldn't find that business")).to_be_visible()


@pytest.mark.allow_http_errors(401)
def test_an_expired_session_mid_chat_goes_back_to_login_with_a_message(page: Page) -> None:
    sign_up(page, "himali-tea")
    page.context.clear_cookies()  # the server no longer sees a session, as after it expired
    page.get_by_label("Message").fill("How long does delivery take?")
    page.get_by_label("Message").press("Enter")
    page.wait_for_url("**/a/himali-tea?reason=expired")
    expect(page.get_by_test_id("session-expired")).to_have_text("Your session expired. Please log in again.")
    expect(page.get_by_role("tab", name="Log in")).to_be_visible()
