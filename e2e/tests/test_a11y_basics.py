"""Accessibility basics: the conversation is a live log, a streaming reply is busy until
it is complete (read once, not token by token), and keyboard focus is visible."""

from playwright.sync_api import Page, expect

from conftest import sign_up


def test_the_reply_is_announced_once_complete_and_focus_is_visible(page: Page) -> None:
    sign_up(page, "himali-tea")
    log = page.get_by_test_id("conversation-log")
    expect(log).to_have_attribute("role", "log")
    expect(log).to_have_attribute("aria-live", "polite")
    page.get_by_label("Message").fill("How long does delivery take?")
    page.get_by_label("Message").press("Enter")
    answer = page.get_by_test_id("assistant-message").last
    expect(answer).to_have_attribute("data-status", "complete")
    expect(answer).to_have_attribute("aria-busy", "false")

    page.get_by_label("Message").fill("one more question")  # Send is enabled only with text
    page.get_by_label("Message").focus()
    page.keyboard.press("Tab")  # from the message box to the Send button, by keyboard
    assert page.evaluate("document.activeElement.textContent.trim()") == "Send"
    outline = page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
    ring = page.evaluate("getComputedStyle(document.activeElement).boxShadow")
    assert outline != "none" or ring != "none", "the focused control shows no focus indicator"
