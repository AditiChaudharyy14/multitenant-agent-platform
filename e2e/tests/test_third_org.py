"""The third org, added with config + seed data only, works end to end in the UI.

Signs up at /a/kathmandu-spice and chats on the scripted fake model: the real
search_knowledge tool runs against this org's own KB, so the citation and its source
passage must come from Kathmandu Spice Co.'s pages, never another org's.
"""

import re

from playwright.sync_api import Page, expect

from conftest import shot, sign_up

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def test_sign_up_at_the_third_org_and_chat(page: Page) -> None:
    sign_up(page, "kathmandu-spice", screenshot="third-org-landing")
    expect(page.get_by_text("Namaste! I'm Masala")).to_be_visible()  # its own greeting from org.yaml
    shot(page, "third-org-chat-empty")

    page.get_by_label("Message").fill("How long does delivery take?")
    page.get_by_label("Message").press("Enter")

    answer = page.get_by_test_id("assistant-message").last
    expect(answer).to_have_attribute("data-status", "complete")
    expect(page).to_have_url(re.compile(rf"/a/kathmandu-spice/chat/{UUID}$"))
    chip = page.get_by_test_id("citation-chip").first
    expect(chip).to_be_visible()
    shot(page, "third-org-chat-answer")

    chip.click()
    drawer = page.get_by_test_id("source-drawer")
    expect(drawer).to_be_visible()
    content = drawer.get_by_test_id("source-content")
    expect(content).to_contain_text(re.compile("working days", re.I))
    # Only this org's KB mentions its own products; never the tea house's or the studio's.
    expect(content).not_to_contain_text(re.compile("tea|massage|yoga", re.I))
