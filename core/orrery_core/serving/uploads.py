"""Read a chat request body, JSON or multipart, without ever touching disk (AEP-030).

Starlette's own form parser spools any file part over 1 MB to a temporary file,
which would put user uploads on the server's disk — the one thing attachments
promise not to do. This parser streams the body through ``python-multipart``
into memory and enforces every bound *while reading*: the total body, each
file, the number of parts and the size of each text field. An oversized upload
is refused at the byte that crosses the limit, not after it has been buffered.

Both chat endpoints accept either body:

- ``application/json`` — ``{"message": ..., "session_id": ...}``, unchanged;
- ``multipart/form-data`` — fields ``message`` and ``session_id``, plus up to
  ``max_files`` parts named ``files``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from python_multipart.multipart import MultipartParser, parse_options_header

from .attachments import AttachmentLimits, Upload

#: The JSON body is a message and a session id; nothing legitimate needs more.
MAX_JSON_BYTES = 64 * 1024
_MAX_MESSAGE_CHARS = 8192
_MAX_FIELD_BYTES = _MAX_MESSAGE_CHARS * 4  # UTF-8 worst case
_FIELDS = frozenset({"message", "session_id"})


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=_MAX_MESSAGE_CHARS)
    session_id: str | None = Field(default=None, max_length=128)


class _MultipartFields(BaseModel):
    """The text fields of a multipart turn: the message may be empty with files."""

    message: str = Field(default="", max_length=_MAX_MESSAGE_CHARS)
    session_id: str | None = Field(default=None, max_length=128)


@dataclass
class ChatInput:
    """A parsed chat body: the message, the session, and any raw uploads."""

    message: str
    session_id: str | None
    uploads: list[Upload] = field(default_factory=list)


def _too_large(detail: str) -> HTTPException:
    return HTTPException(status_code=413, detail=detail)


def _validation_error(exc: ValidationError) -> RequestValidationError:
    # The same 422 shape FastAPI gives a body model, so clients see no change.
    return RequestValidationError(
        [{**e, "loc": ("body", *e["loc"])} for e in exc.errors(include_url=False)]
    )


async def _read_capped(request: Request, cap: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        raise _too_large(f"Request body is over the {cap // 1024} KB limit.")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > cap:
            raise _too_large(f"Request body is over the {cap // 1024} KB limit.")
    return bytes(body)


async def read_chat_input(request: Request, limits: AttachmentLimits) -> ChatInput:
    """Parse a chat body by its content type. Raises 413/415/422 ``HTTPException``s."""
    ctype, params = parse_options_header(request.headers.get("content-type", ""))
    if ctype == b"multipart/form-data":
        if not limits.active:
            raise HTTPException(
                status_code=415, detail="Attachments are disabled on this deployment."
            )
        boundary = params.get(b"boundary")
        if not boundary:
            raise HTTPException(status_code=422, detail="Multipart body without a boundary.")
        return await _read_multipart(request, boundary, limits)
    if ctype not in (b"application/json", b""):
        raise HTTPException(
            status_code=415, detail="Send application/json, or multipart/form-data with files."
        )
    raw = await _read_capped(request, MAX_JSON_BYTES)
    try:
        payload: Any = json.loads(raw or b"null")
    except ValueError as exc:
        raise RequestValidationError(
            [{"type": "json_invalid", "loc": ("body",), "msg": "Invalid JSON", "input": None}]
        ) from exc
    try:
        body = ChatRequest.model_validate(payload)
    except ValidationError as exc:
        raise _validation_error(exc) from exc
    return ChatInput(message=body.message, session_id=body.session_id)


class _Collector:
    """Callback target for :class:`MultipartParser`: parts into memory, under caps."""

    def __init__(self, limits: AttachmentLimits) -> None:
        self.limits = limits
        self.fields: dict[str, str] = {}
        self.uploads: list[Upload] = []
        self._header_field = bytearray()
        self._header_value = bytearray()
        self._headers: dict[bytes, bytes] = {}
        self._name = ""
        self._filename: str | None = None
        self._data = bytearray()
        self._cap = 0

    def on_part_begin(self) -> None:
        self._headers = {}
        self._data = bytearray()

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_field += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def on_header_end(self) -> None:
        self._headers[bytes(self._header_field).lower()] = bytes(self._header_value)
        self._header_field.clear()
        self._header_value.clear()

    def on_headers_finished(self) -> None:
        disposition, opts = parse_options_header(self._headers.get(b"content-disposition", b""))
        if disposition != b"form-data" or b"name" not in opts:
            raise HTTPException(status_code=422, detail="Malformed multipart part.")
        self._name = opts[b"name"].decode("utf-8", "replace")
        filename = opts.get(b"filename")
        self._filename = filename.decode("utf-8", "replace") if filename is not None else None
        if self._name == "files":
            if len(self.uploads) >= self.limits.max_files:
                raise _too_large(f"At most {self.limits.max_files} files per message.")
            self._cap = self.limits.max_file_bytes
        elif self._name in _FIELDS and self._filename is None:
            if self._name in self.fields:
                raise HTTPException(status_code=422, detail=f"Field {self._name!r} repeated.")
            self._cap = _MAX_FIELD_BYTES
        else:
            raise HTTPException(status_code=422, detail=f"Unexpected form field {self._name!r}.")

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        self._data += data[start:end]
        if len(self._data) > self._cap:
            if self._name == "files":
                raise _too_large(
                    f"{self._filename or 'A file'} is over the "
                    f"{self.limits.max_file_bytes // (1024 * 1024)} MB per-file limit."
                )
            raise _too_large(f"Field {self._name!r} is too long.")

    def on_part_end(self) -> None:
        if self._name == "files":
            self.uploads.append(Upload(filename=self._filename or "", data=bytes(self._data)))
        else:
            try:
                self.fields[self._name] = self._data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HTTPException(
                    status_code=422, detail=f"Field {self._name!r} is not UTF-8."
                ) from exc
        self._data = bytearray()


async def _read_multipart(request: Request, boundary: bytes, limits: AttachmentLimits) -> ChatInput:
    cap = limits.max_total_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        raise _too_large(f"Request body is over the {cap // (1024 * 1024)} MB limit.")
    collector = _Collector(limits)
    callbacks: Any = {
        name: getattr(collector, name)
        for name in (
            "on_part_begin",
            "on_part_data",
            "on_part_end",
            "on_header_field",
            "on_header_value",
            "on_header_end",
            "on_headers_finished",
        )
    }
    parser = MultipartParser(boundary, callbacks, max_size=cap)
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > cap:
                raise _too_large(f"Request body is over the {cap // (1024 * 1024)} MB limit.")
            parser.write(chunk)
        parser.finalize()
    except HTTPException:
        raise
    except Exception as exc:  # python-multipart's parse errors on a malformed body
        raise HTTPException(status_code=422, detail="Malformed multipart body.") from exc
    try:
        fields = _MultipartFields.model_validate(collector.fields)
    except ValidationError as exc:
        raise _validation_error(exc) from exc
    if not fields.message.strip() and not collector.uploads:
        raise HTTPException(status_code=422, detail="Send a message, a file, or both.")
    return ChatInput(
        message=fields.message.strip(),
        session_id=fields.session_id or None,
        uploads=collector.uploads,
    )
