from __future__ import annotations


class CanvasError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        url: str | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url
        self.body = body


class CanvasAuthError(CanvasError):
    pass


class CanvasForbiddenError(CanvasError):
    pass


class CanvasNotFoundError(CanvasError):
    pass


class CanvasRateLimitError(CanvasError):
    pass


class CanvasServerError(CanvasError):
    pass


class CanvasNetworkError(CanvasError):
    pass
