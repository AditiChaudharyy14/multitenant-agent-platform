"""argon2id password hashing (argon2-cffi's PasswordHasher defaults are argon2id).

Hashing is deliberately slow and CPU-bound, so it runs in a worker thread instead of
blocking the event loop.
"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from starlette.concurrency import run_in_threadpool

_hasher = PasswordHasher()

# Verified against when the email is unknown, so a miss costs the same time as a hit
# and response timing doesn't reveal which emails exist.
DUMMY_HASH = _hasher.hash("dummy password, never matches anything real")


async def hash_password(password: str) -> str:
    return await run_in_threadpool(_hasher.hash, password)


async def verify_password(password_hash: str, password: str) -> bool:
    def _verify() -> bool:
        try:
            return _hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):  # mismatch or corrupt hash
            return False

    return await run_in_threadpool(_verify)


def needs_rehash(password_hash: str) -> bool:
    """True when the hash used weaker or different parameters than the current defaults."""
    return _hasher.check_needs_rehash(password_hash)
