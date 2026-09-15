"""HTTP/problem-style errors for platform services."""

from typing import Any


class PlatformError(Exception):
    def __init__(
        self,
        title: str,
        *,
        status: int = 400,
        detail: str | None = None,
        error_code: str = "platform_error",
        type_: str = "about:blank",
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail or title)
        self.title = title
        self.status = status
        self.detail = detail or title
        self.error_code = error_code
        self.type = type_
        self.extra = extra or {}

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": self.type,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "error_code": self.error_code,
        }
        body.update(self.extra)
        return body


class NotFoundError(PlatformError):
    def __init__(self, detail: str = "Resource not found", **kwargs: Any) -> None:
        super().__init__("Not Found", status=404, detail=detail, error_code="not_found", **kwargs)


class ConflictError(PlatformError):
    def __init__(self, detail: str, error_code: str = "conflict", **kwargs: Any) -> None:
        super().__init__("Conflict", status=409, detail=detail, error_code=error_code, **kwargs)


class UnauthorizedError(PlatformError):
    def __init__(self, detail: str = "Unauthorized", **kwargs: Any) -> None:
        super().__init__(
            "Unauthorized", status=401, detail=detail, error_code="unauthorized", **kwargs
        )


class ForbiddenError(PlatformError):
    def __init__(self, detail: str = "Forbidden", **kwargs: Any) -> None:
        super().__init__("Forbidden", status=403, detail=detail, error_code="forbidden", **kwargs)
