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

from app.core import metrics


class AppError(HTTPException):
    """Base for the errors this API raises deliberately."""

    def __init__(
        self,
        message: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        code: str = "bad_request",
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code
        # Merged into the response body by the handler. camelCase, matching
        # `statusMessage` and `retryAfter`, because the Nuxt client reads it.
        # Keys with a None value are dropped, so a caller can pass a field it
        # does not always know.
        self.extra = {k: v for k, v in (extra or {}).items() if v is not None}


class BadRequestError(AppError):
    def __init__(self, message: str, code: str = "bad_request") -> None:
        super().__init__(message, status.HTTP_400_BAD_REQUEST, code)


class UnauthorizedError(AppError):
    def __init__(
        self,
        message: str,
        code: str = "unauthorized",
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            status.HTTP_401_UNAUTHORIZED,
            code,
            headers={"WWW-Authenticate": "Bearer"},
            # Used by the internal request-signing check to report our clock.
            # Clock skew is the commonest cross-team integration failure and is
            # undebuggable without it; the server's time is already in the
            # `Date` header, so this reveals nothing new.
            extra=extra,
        )


class PaymentRequiredError(AppError):
    """The wallet cannot cover this.

    Distinct from 403 on purpose: the caller is not forbidden from doing this,
    they just have to top up first, and the Nuxt app branches on the status to
    open the top-up dialog rather than an error page.

    The shortfall is in the body so the client can say "top up at least X"
    instead of making the user guess.
    """

    def __init__(
        self,
        message: str,
        code: str = "insufficient_balance",
        *,
        required_micros: int | None = None,
        available_micros: int | None = None,
        shortfall_micros: int | None = None,
    ) -> None:
        super().__init__(
            message,
            status.HTTP_402_PAYMENT_REQUIRED,
            code,
            extra={
                "requiredMicros": required_micros,
                "availableMicros": available_micros,
                "shortfallMicros": shortfall_micros,
            },
        )
        self.required_micros = required_micros
        self.available_micros = available_micros
        self.shortfall_micros = shortfall_micros


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
        super().__init__(
            message,
            status.HTTP_429_TOO_MANY_REQUESTS,
            code,
            headers=headers,
            extra={"retryAfter": retry_after},
        )
        self.retry_after = retry_after


class BadGatewayError(AppError):
    """A provider we depend on was unreachable or answered with nonsense."""

    def __init__(self, message: str, code: str = "bad_gateway") -> None:
        super().__init__(message, status.HTTP_502_BAD_GATEWAY, code)


class ServiceUnavailableError(AppError):
    def __init__(
        self,
        message: str,
        code: str = "service_unavailable",
        retry_after: int | None = None,
    ) -> None:
        # `retry_after` because one 503 here is genuinely temporary and the
        # others are not. `stt_not_ready` means a checkpoint is loading and the
        # call succeeds on its own in a few seconds; `tts_not_configured` and
        # `stt_key_rejected` need a human, and telling a client to retry those
        # is telling it to hammer a wall. Only the first one passes a number.
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
        super().__init__(
            message,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            code,
            headers=headers,
            extra={"retryAfter": retry_after},
        )
        self.retry_after = retry_after


def _body(message: str, code: str, **extra: Any) -> dict[str, Any]:
    return {"detail": message, "statusMessage": message, "code": code, **extra}


def register_exception_handlers(app: FastAPI) -> None:
    # Counted here rather than in the metrics middleware, which sees a status
    # and nothing else. `402` alone cannot tell "top up your balance" from
    # "your card is frozen", and `503` cannot tell an unconfigured deployment
    # from a rejected upstream key — but `code` names exactly one of them, and
    # it is the same string the client branches on. Every value is a constant
    # from this module or its callers, so the label set stays closed.
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        metrics.record_api_error(code=exc.code, status=exc.status_code)
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(str(exc.detail), exc.code, **exc.extra),
            headers=exc.headers,
        )

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
        metrics.record_api_error(code="http_error", status=exc.status_code)
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

        metrics.record_api_error(
            code="validation_error", status=status.HTTP_422_UNPROCESSABLE_ENTITY
        )
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
