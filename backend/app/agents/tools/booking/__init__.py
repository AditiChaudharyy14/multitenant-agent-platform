"""Booking-vertical tools (registered for org.vertical == 'booking')."""

from app.agents.tools.booking.bookings import cancel_booking, create_booking, list_my_bookings, reschedule_booking
from app.agents.tools.booking.catalog import find_available_slots, get_opening_hours, list_services

BOOKING_TOOLS = [list_services, get_opening_hours, find_available_slots, create_booking, reschedule_booking,
                 cancel_booking, list_my_bookings]  # fmt: skip

__all__ = ["BOOKING_TOOLS", "cancel_booking", "create_booking", "find_available_slots", "get_opening_hours",
           "list_my_bookings", "list_services", "reschedule_booking"]  # fmt: skip
