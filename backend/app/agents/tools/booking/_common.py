"""Booking argument types. Names are matched leniently on the server ("swedish" finds
"Swedish Relaxation Massage"); ambiguous or unknown names come back as errors to ask about."""

from typing import Annotated

from pydantic import Field

ServiceName = Annotated[str, Field(min_length=1, max_length=120, description="Service name as list_services shows it.")]
PractitionerName = Annotated[
    str, Field(min_length=1, max_length=120, description="Practitioner name; omit if the customer has no preference.")
]
Start = Annotated[
    str,
    Field(min_length=16, max_length=32, description="Start in the studio's local time, YYYY-MM-DDTHH:MM, exactly as "
                                                    "find_available_slots returned it, e.g. 2026-11-10T14:00."),
]  # fmt: skip
BookingNumber = Annotated[
    str, Field(min_length=1, max_length=30, description="The booking number the customer sees, e.g. LW-1001.")
]
