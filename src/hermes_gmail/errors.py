"""The closed set of error categories every caller sees (docs/design.md, Errors)."""

from enum import StrEnum


class Category(StrEnum):
    VAULT_UNAVAILABLE = "vault_unavailable"
    OAUTH_INVALID_GRANT = "oauth_invalid_grant"
    OAUTH_INVALID_CLIENT = "oauth_invalid_client"
    OAUTH_FAILED = "oauth_failed"
    GMAIL_QUOTA = "gmail_quota"
    GMAIL_SCOPE = "gmail_scope"
    GMAIL_NOT_FOUND = "gmail_not_found"
    GMAIL_FAILED = "gmail_failed"
    REFUSED = "refused"
    DECLINED = "declined"
    UNCERTAIN = "uncertain"


class GateError(Exception):
    """An outcome a caller can act on.

    `detail` is a fixed sentence written in this package. It never carries a provider
    response body, a token, a vault message or mail content.
    """

    def __init__(self, category: Category, detail: str) -> None:
        super().__init__(f"{category}: {detail}")
        self.category = category
        self.detail = detail
        self.counted = False  # set once health() has counted it

    def to_dict(self) -> dict[str, str]:
        return {"category": str(self.category), "detail": self.detail}
