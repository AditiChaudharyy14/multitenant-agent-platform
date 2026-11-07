"""Branded email bodies, built from org config (name, colours, emoji, agent name).

Nothing here names an org. Plain text and HTML carry the same facts; user-supplied text
(names, addresses) is HTML-escaped. Times are shown in the org's timezone with the
absolute date, like the confirmation cards.
"""

from dataclasses import dataclass
from html import escape

from app.tenancy.orgs import Org


@dataclass(frozen=True, slots=True)
class Email:
    subject: str
    text: str
    html: str


def _brand(org: Org) -> tuple[str, str, str, str]:
    b = org.branding
    return (b.get("logo_emoji", ""), b.get("agent_name", "the assistant"), b.get("primary_color", "#333333"),
            b.get("secondary_color", "#999999"))  # fmt: skip


def _render(org: Org, subject: str, greeting: str, lines: list[tuple[str, str]], closing: str) -> Email:
    emoji, agent, primary, secondary = _brand(org)
    text = "\n".join([
        greeting, "", *(f"{k}: {v}" for k, v in lines), "", closing, "",
        f"{agent}, {org.name}", "This is an automated message; reply in the chat if you need anything.",
    ])  # fmt: skip
    rows = "".join(
        f'<tr><td style="padding:4px 12px 4px 0;color:#666">{escape(k)}</td><td style="padding:4px 0"><strong>{escape(v)}</strong></td></tr>'
        for k, v in lines
    )
    html = f"""<!doctype html><html><body style="margin:0;background:#f6f4ef;font-family:Helvetica,Arial,sans-serif;color:#222">
<div style="max-width:560px;margin:24px auto;background:#fff;border-top:6px solid {escape(primary)};border-radius:8px;overflow:hidden">
<div style="padding:20px 24px;border-bottom:1px solid {escape(secondary)}"><span style="font-size:28px">{escape(emoji)}</span>
<span style="font-size:20px;font-weight:bold;color:{escape(primary)};vertical-align:middle;margin-left:8px">{escape(org.name)}</span></div>
<div style="padding:20px 24px"><p>{escape(greeting)}</p><table style="border-collapse:collapse;font-size:15px">{rows}</table>
<p>{escape(closing)}</p><p style="color:#666;font-size:13px">{escape(agent)}, {escape(org.name)}<br>
This is an automated message; reply in the chat if you need anything.</p></div></div></body></html>"""
    return Email(f"{subject} {emoji}".strip(), text, html)


def order_shipped(org: Org, *, name: str, number: str, items: list[str], address: str, total: str) -> Email:
    return _render(
        org, f"Your order {number} has shipped", f"Namaste {name}, good news: your order {number} is on its way.",
        [("Order", number), ("Items", ", ".join(items)), ("Delivering to", address),
         ("To pay on delivery", f"{total} (cash on delivery)")],
        "Our rider will call before arriving. You can ask the assistant for the live status at any time.",
    )  # fmt: skip


def booking_confirmed(org: Org, *, name: str, number: str, service: str, practitioner: str, when: str) -> Email:
    return _render(
        org, f"Booking {number} confirmed: {service}, {when}", f"Namaste {name}, your booking is confirmed.",
        [("Booking", number), ("Service", service), ("With", practitioner), ("When", f"{when} ({org.timezone})")],
        "Cancellation is free until 24 hours before the start; after that a 50% fee applies.",
    )  # fmt: skip


def booking_reminder(org: Org, *, name: str, number: str, service: str, practitioner: str, when: str) -> Email:
    return _render(
        org, f"Reminder: {service} on {when} ({number})", f"Namaste {name}, a reminder of your appointment in the next 24 hours.",
        [("Booking", number), ("Service", service), ("With", practitioner), ("When", f"{when} ({org.timezone})")],
        "Please arrive ten minutes early. Need to change it? Ask the assistant in the chat.",
    )  # fmt: skip
