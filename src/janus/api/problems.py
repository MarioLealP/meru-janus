"""Errors as RFC 9457 problem details (application/problem+json), the same shape Thoth answers with.

`detail` keeps the text an HTTPException carried, so a client that only reads `detail` or the status sees no change.
A request body that doesn't validate used to answer FastAPI's `detail` list; the list now lives in `errors` (the same
loc/msg/type items) and `detail` is a sentence. Shikigami's verdict on a proposal (422, `problems` +
`checked_against`) is an existing contract with the janus CLI and stays as it is (see requests.submit).
"""

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException

MEDIA_TYPE = "application/problem+json"


class Problem(BaseModel):
    """What went wrong (RFC 9457 problem details)."""

    model_config = ConfigDict(frozen=True)

    type: str = Field(default="about:blank", examples=["about:blank"])
    title: str = Field(examples=["Not Found"])
    status: int = Field(examples=[404])
    detail: str | None = Field(default=None, examples=["can't read Meru/requests: ConnectError"])
    errors: list[dict[str, Any]] | None = Field(  # field-level details, for validation failures
        default=None, examples=[[{"loc": ["body", "key"], "msg": "Field required", "type": "missing"}]]
    )


def response(problem: Problem, headers: dict[str, str] | None = None) -> JSONResponse:
    """The problem as an application/problem+json answer."""
    return JSONResponse(
        problem.model_dump(exclude_none=True), status_code=problem.status, media_type=MEDIA_TYPE, headers=headers
    )


def _http_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, HTTPException)
    problem = Problem(title=HTTPStatus(exc.status_code).phrase, status=exc.status_code, detail=exc.detail)
    return response(problem, getattr(exc, "headers", None))


def _validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
    return response(Problem(title="Unprocessable Content", status=422, detail="the request is invalid", errors=errors))


def install(app: FastAPI) -> None:
    """Make `app` answer every error as problem details."""
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
