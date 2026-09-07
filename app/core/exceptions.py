"""A single error shape for the whole API.

The Nuxt frontend reads `error.data.statusMessage` (an h3 convention), so every
error body carries the message under both `detail` and `statusMessage`. That
lets the existing pages talk to this backend without touching their error
handling.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppError(HTTPException):
    """Base for the errors this API raises deliberately."""

    def __init__(
        self,
        message: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        code: str = "bad_request",
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code


class BadRequestError(AppError):
    def __init__(self, message: str, code: str = "bad_request") -> None:
        super().__init__(message, status.HTTP_400_BAD_REQUEST, code)


class UnauthorizedError(AppError):
    def __init__(self, message: str, code: str = "unauthorized") -> None:
        super().__init__(
            message,
            status.HTTP_401_UNAUTHORIZED,
            code,
            headers={"WWW-Authenticate": "Bearer"},
        )


class ForbiddenError(AppError):
    def __init__(self, message: str, code: str = "forbidden") -> None:
        super().__init__(message, status.HTTP_403_FORBIDDEN, code)


class NotFoundError(AppError):
    def __init__(self, message: str, code: str = "not_found") -> None:
        super().__init__(message, status.HTTP_404_NOT_FOUND, code)


class ConflictError(AppError):
    def __init__(self, message: str, code: str = "conflict") -> None:
        super().__init__(message, status.HTTP_409_CONFLICT, code)


class TooManyRequestsError(AppError):
    def __init__(self, message: str, code: str = "too_many_requests", retry_after: int | None = None) -> None:
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
        super().__init__(message, status.HTTP_429_TOO_MANY_REQUESTS, code, headers=headers)
        self.retry_after = retry_after


class BadGatewayError(AppError):
    """A provider we depend on was unreachable or answered with nonsense."""

    def __init__(self, message: str, code: str = "bad_gateway") -> None:
        super().__init__(message, status.HTTP_502_BAD_GATEWAY, code)


class ServiceUnavailableError(AppError):
    def __init__(self, message: str, code: str = "service_unavailable") -> None:
        super().__init__(message, status.HTTP_503_SERVICE_UNAVAILABLE, code)


def _body(message: str, code: str, **extra: Any) -> dict[str, Any]:
    return {"detail": message, "statusMessage": message, "code": code, **extra}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        extra = {"retryAfter": exc.retry_after} if isinstance(exc, TooManyRequestsError) else {}
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(str(exc.detail), exc.code, **extra),
            headers=exc.headers,
        )

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(str(exc.detail), "http_error"),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        first = errors[0] if errors else {}
        field = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
        message = first.get("msg", "The request payload is invalid.")
        if field:
            message = f"{field}: {message}"

        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_body(
                message,
                "validation_error",
                errors=[
                    {
                        "field": ".".join(str(p) for p in err.get("loc", ()) if p != "body"),
                        "message": err.get("msg", ""),
                    }
                    for err in errors
                ],
            ),
        )
