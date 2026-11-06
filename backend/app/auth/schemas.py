"""Request bodies for auth. Unknown fields (role, org_id, user_id, ...) are ignored, so a
client can never choose its own role or org."""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

MIN_PASSWORD_LENGTH = 10
# argon2 accepts any length; a cap keeps one request from hashing megabytes.
MAX_PASSWORD_LENGTH = 1024
# A shape check only. Deliverability isn't verified, and email-validator isn't worth a
# dependency here. Case is handled by the citext column.
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 254 or not _EMAIL_SHAPE.match(email):
        raise ValueError("invalid email address")
    return email


class SignupIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    email: str
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
    display_name: str = Field(min_length=1, max_length=80)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return normalize_email(v)

    @field_validator("display_name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("display_name must not be blank")
        return v.strip()


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # No shape check: a malformed email must fail exactly like a wrong one (401).
    email: str = Field(max_length=254)
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()
