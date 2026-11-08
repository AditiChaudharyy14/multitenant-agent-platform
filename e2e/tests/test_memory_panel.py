"""Long-term memory in the UI: the agent saves a preference with its real tool, the
"What I remember" section lists it, and its Forget button deletes it for good."""

from playwright.sync_api import Page, expect

from conftest import shot, sign_up
from test_cards_and_panels import open_panel


def test_remembered_preference_shows_and_can_be_forgotten(page: Page) -> None:
    sign_up(page, "himali-tea")
    panel = open_panel(page, "My orders")
    expect(panel.get_by_test_id("memories")).to_contain_text("Nothing yet")
    if page.viewport_size["width"] < 1100:
        page.keyboard.press("Escape")

    page.get_by_label("Message").fill("Please remember I take oat milk")
    page.get_by_label("Message").press("Enter")
    expect(page.get_by_test_id("assistant-message").last).to_have_attribute("data-status", "complete")

    panel = open_panel(page, "My orders")
    row = panel.get_by_test_id("memory-row")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("Milk")
    expect(row).to_contain_text("oat milk")
    expect(row).to_contain_text("you told me")
    shot(page, "panel-memories")

    row.get_by_role("button", name="Forget Milk").click()
    expect(panel.get_by_test_id("memory-row")).to_have_count(0)
    page.reload()  # gone on the server, not just hidden
    panel = open_panel(page, "My orders")
    expect(panel.get_by_test_id("memories")).to_contain_text("Nothing yet")


def test_admin_on_the_customer_chat_has_no_memory_section(page: Page) -> None:
    """Memory is private to customers: the API 404s an admin, so the UI must not ask
    (the page fails on any console error)."""
    from test_admin_analytics import ADMIN_EMAIL, ADMIN_PASSWORD, log_in

    log_in(page, "himali-tea", ADMIN_EMAIL, ADMIN_PASSWORD)
    panel = open_panel(page, "My orders")
    expect(panel).to_be_visible()
    expect(page.get_by_test_id("memories")).to_have_count(0)
