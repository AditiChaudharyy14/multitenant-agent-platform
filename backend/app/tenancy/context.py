"""ToolContext: who is acting, built by the server from the session and injected into tools.

Never constructed from anything in the request itself (URL, headers, query, body).
"""

from dataclasses import dataclass
from uuid import UUID

from app.db.session import TenantContext


@dataclass(frozen=True, slots=True)
class ToolContext:
    org_id: UUID
    user_id: UUID
    role: str  # 'customer' | 'admin'
    # Known only inside a chat turn; None for plain API requests.
    conversation_id: UUID | None = None
    turn_id: UUID | None = None

    @property
    def tenant(self) -> TenantContext:
        """The subset tenant_tx/readonly_tx need to scope a transaction."""
        return TenantContext(org_id=self.org_id, user_id=self.user_id, role=self.role)
