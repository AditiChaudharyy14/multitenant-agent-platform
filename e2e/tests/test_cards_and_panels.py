"""Rich cards, the sidebar and the My orders / My bookings panel, against the running stack
(scripted fake LLM, real tools, real database). Runs at 1280 px and at 375 px."""

import re

from playwright.sync_api import Page, expect

from conftest import shot, sign_up

PANEL_WIDTH = 1100  # at this width and above the panel is inline; below it opens from a header button


def ask(page: Page, text: str):  # noqa: ANN201
    """Send a message and wait until the assistant's reply is complete. Returns that reply."""
    replies = page.get_by_test_id("assistant-message")
    before = replies.count()
    box = page.get_by_label("Message")
    box.fill(text)
    box.press("Enter")
    expect(replies).to_have_count(before + 1)
    expect(replies.last).to_have_attribute("data-status", "complete")
    return replies.last


def narrow(page: Page) -> bool:
    return page.viewport_size["width"] < PANEL_WIDTH


def open_panel(page: Page, title: str):  # noqa: ANN201
    """The My orders / My bookings panel, opening its drawer first on a narrow screen."""
    if narrow(page):
        page.get_by_role("button", name=title, exact=True).click()
    panel = page.locator("[data-testid=my-items]:visible")
    expect(panel).to_be_visible()
    return panel


def close_panel(page: Page) -> None:
    if narrow(page):
        page.keyboard.press("Escape")
        expect(page.locator("[data-testid=my-items]:visible")).to_have_count(0)


def open_chats(page: Page) -> None:
    """The chat list: always visible on a wide screen, a drawer at 768 px and below."""
    if page.viewport_size["width"] <= 768:
        page.get_by_role("button", name="Chats", exact=True).click()
    expect(chat_nav(page)).to_be_visible()


def chat_nav(page: Page):  # noqa: ANN201
    """The visible chat list: the sidebar, or the drawer on a narrow screen."""
    return page.locator("nav[aria-label=Chats]:visible")


def no_sideways_scroll(page: Page) -> None:
    sizes = page.evaluate("({sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth})")
    assert sizes["sw"] <= sizes["cw"], f"page scrolls sideways: {sizes}"
    # The composer stays on screen however long the chat gets.
    box = page.get_by_label("Message").bounding_box()
    assert box and 0 <= box["y"] and box["y"] + box["height"] <= page.viewport_size["height"], box


def test_store_order_card_confirm_and_my_orders(page: Page) -> None:
    sign_up(page, "himali-tea")
    ask(page, "I'd like to order two sencha please")
    card = page.get_by_test_id("proposal-card").last
    expect(card).to_have_attribute("data-state", "open")
    expect(card).to_contain_text("2 × Himalayan Sencha")
    expect(card).to_contain_text("House 7, Baluwatar")
    expect(card.get_by_test_id("proposal-total")).to_be_visible()
    shot(page, "card-order-summary")
    no_sideways_scroll(page)

    card.get_by_role("button", name="Confirm order").click()
    expect(card).to_have_attribute("data-state", "confirmed")
    expect(card.get_by_test_id("confirmed-tag")).to_have_text("Confirmed ✓")
    expect(card.get_by_role("button")).to_have_count(0)
    outcome = page.get_by_test_id("outcome-card").last
    expect(outcome).to_contain_text("Order placed")
    expect(outcome).to_contain_text("Pending")  # confirming makes a pending order, not a confirmed one
    shot(page, "card-order-placed")

    panel = open_panel(page, "My orders")
    rows = panel.get_by_test_id("order-row")
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("Pending")
    number = re.search(r"HT-\d+", rows.first.inner_text()).group(0)
    shot(page, "panel-orders")
    close_panel(page)

    # History reloads with the card's real, current state.
    page.reload()
    again = page.get_by_test_id("proposal-card").last
    expect(again).to_have_attribute("data-state", "confirmed")
    expect(again.get_by_test_id("confirmed-tag")).to_be_visible()

    expect(ask(page, "show me my orders").get_by_test_id("order-list")).to_contain_text(number)
    shot(page, "card-order-list")
    expect(ask(page, "what teas do you have?").get_by_test_id("product-list")).to_contain_text("Himalayan Sencha")
    shot(page, "card-product-list")
    status = ask(page, f"what's the status of order {number}").get_by_test_id("order-status")
    expect(status).to_contain_text("Order placed")
    shot(page, "card-order-status")

    # A newer summary supersedes the older one: the old card greys out.
    ask(page, f"please cancel order {number}")
    cancel = page.locator("[data-testid=proposal-card][data-kind=cancel_order]")  # not .last: a newer card is coming
    expect(cancel).to_have_attribute("data-state", "open")
    shot(page, "card-order-cancel")
    ask(page, "I'd like to order two sencha please")
    expect(cancel).to_have_attribute("data-state", "superseded")
    expect(cancel.get_by_test_id("proposal-note")).to_contain_text("newer summary")
    expect(cancel.get_by_role("button")).to_have_count(0)
    shot(page, "card-superseded")
    no_sideways_scroll(page)


def test_booking_slot_chip_confirm_and_my_bookings(page: Page) -> None:
    sign_up(page, "lotus-wellness")
    ask(page, "what times are free this week?")
    slots = page.get_by_test_id("slot-list").last
    chips = slots.get_by_test_id("slot-chip")
    expect(chips.first).to_be_enabled()
    assert chips.count() > 1
    shot(page, "card-slot-list")
    no_sideways_scroll(page)

    chips.first.click()
    expect(chips.first).to_be_disabled()  # disabled while the turn streams
    card = page.get_by_test_id("proposal-card").last
    expect(card).to_have_attribute("data-state", "open")
    expect(card).to_contain_text("Swedish Relaxation Massage")
    expect(card).to_contain_text("Date")
    shot(page, "card-booking-summary")

    card.get_by_role("button", name="Confirm booking").click()
    expect(card).to_have_attribute("data-state", "confirmed")
    expect(card.get_by_test_id("confirmed-tag")).to_have_text("Confirmed ✓")
    expect(card.get_by_role("button")).to_have_count(0)
    expect(page.get_by_test_id("outcome-card").last).to_contain_text("Booking made")
    shot(page, "card-booking-made")

    panel = open_panel(page, "My bookings")
    rows = panel.get_by_test_id("booking-row")
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("Swedish Relaxation Massage")
    number = re.search(r"LW-\d+", rows.first.inner_text()).group(0)
    shot(page, "panel-bookings")
    close_panel(page)

    page.reload()
    expect(page.get_by_test_id("proposal-card").last).to_have_attribute("data-state", "confirmed")

    expect(ask(page, "show my bookings").get_by_test_id("booking-list")).to_contain_text(number)
    shot(page, "card-booking-list")
    expect(ask(page, "what services do you offer?").get_by_test_id("service-list")).to_be_visible()
    shot(page, "card-service-list")
    ask(page, f"cancel booking {number}")
    expect(page.get_by_test_id("proposal-card").last).to_have_attribute("data-kind", "cancel_booking")
    expect(page.get_by_test_id("proposal-card").last).to_contain_text("Fee")
    shot(page, "card-booking-cancel")
    no_sideways_scroll(page)


def test_rename_and_delete_a_conversation(page: Page) -> None:
    sign_up(page, "himali-tea")
    ask(page, "How long does delivery take?")
    open_chats(page)
    shot(page, "sidebar")
    nav = chat_nav(page)
    item = nav.get_by_test_id("conversation-item").first
    expect(item.get_by_role("button", name="How long does delivery take?", exact=True)).to_have_attribute("aria-current", "page")

    item.hover()
    item.get_by_role("button", name=re.compile(r"^Rename")).click()
    field = nav.get_by_label("Chat name")
    field.fill("Delivery times")
    field.press("Enter")
    expect(nav.get_by_test_id("conversation-item").first).to_contain_text("Delivery times")

    page.reload()  # the new name is stored, not just shown
    open_chats(page)
    nav = chat_nav(page)
    item = nav.get_by_test_id("conversation-item").first
    expect(item).to_contain_text("Delivery times")

    item.hover()
    item.get_by_role("button", name=re.compile(r"^Delete")).click()
    dialog = page.get_by_role("alertdialog")
    expect(dialog).to_contain_text("Delete this chat?")
    shot(page, "delete-dialog")
    dialog.get_by_role("button", name="Keep chat").click()
    expect(dialog).to_have_count(0)
    expect(item).to_be_visible()

    item.hover()
    item.get_by_role("button", name=re.compile(r"^Delete")).click()
    page.get_by_role("alertdialog").get_by_role("button", name="Delete chat").click()
    expect(nav.get_by_test_id("conversation-item")).to_have_count(0)
    page.wait_for_url(re.compile(r"/a/himali-tea/chat$"))  # deleting the open chat leaves it
    if page.viewport_size["width"] <= 768:
        expect(page.get_by_role("button", name="Chats", exact=True)).to_be_visible()
    expect(page.get_by_text("Namaste! I'm Chiya")).to_be_visible()
