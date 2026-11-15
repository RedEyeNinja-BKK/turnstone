"""HTTP handlers for user avatars, mounted on both the node server and the console.

Both processes read the same database, so the same handlers serve whichever UI
the browser is on and an ``<img src="/v1/api/users/{id}/avatar?v=...">`` works
in every UI without a node prefix.

* ``GET /v1/api/users/{user_id}/avatar``: the uploaded picture as PNG, for any
  signed-in caller (``user_id`` values are already visible to every signed-in
  user).  ``?v=`` equal to the current picture version makes the response
  cacheable for a year; anything else revalidates against the ETag.
* ``PUT /v1/api/users/{user_id}/avatar``: ``{"color": 0-5}``.
* ``PUT /v1/api/users/{user_id}/avatar/picture``: multipart ``file`` field.
* ``DELETE /v1/api/users/{user_id}/avatar/picture``.

The three writes change only the caller's own account and need the ``write``
scope (see ``required_scope``): read means no writes, so a viewer keeps the
colour picked when their account was made.  Storing or removing a picture is
audited (``user.avatar.set_picture``, ``user.avatar.remove_picture``) with the
picture's version.  Admins remove someone else's picture through the console's
admin route.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import anyio
import anyio.to_thread
from starlette.responses import JSONResponse, Response

from turnstone.core.avatars import (
    MAX_PICTURE_BYTES,
    PALETTE_SIZE,
    PictureRejectedError,
    descriptor_for_user,
    normalize_picture,
    picture_version,
)
from turnstone.core.web_helpers import (
    audit_context,
    auth_user_id,
    read_json_or_400,
    read_multipart_file_or_400,
    require_storage_or_503,
)

if TYPE_CHECKING:
    from starlette.requests import Request

_PICTURE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
}
_CACHE_VERSIONED = "private, max-age=31536000, immutable"
_CACHE_REVALIDATE = "private, no-cache"

# Pictures decode one at a time per process, in arrival order.  Each person has
# one upload at a time, held from the first byte of its body, so nobody's
# parallel uploads are read before they are refused.  At most this many uploads
# are past their read, waiting or decoding; counting them apart from those still
# sending means slow senders can't hold the places.  The rest are refused (503)
# rather than held.
_DECODE_LIMITER = anyio.CapacityLimiter(1)
_MAX_UPLOADS_IN_FLIGHT = 5

# The people with an upload under way, and those of them past their read.  Each
# is checked and filled with nothing awaited in between, so uploads that arrive
# together can't all pass.
_uploading: set[str] = set()
_admitted: set[str] = set()

_GONE = "This account no longer exists"


def _own_account(request: Request, storage: Any) -> dict[str, Any] | JSONResponse:
    """The caller's users row when the path names the caller, else an error response."""
    user_id = request.path_params["user_id"]
    if not user_id or user_id != auth_user_id(request):
        return JSONResponse({"error": "You can change only your own avatar"}, status_code=403)
    user = storage.get_user(user_id)
    if not isinstance(user, dict):
        # Service principals have no users row, so no avatar to set.
        return JSONResponse({"error": "Only user accounts have avatars"}, status_code=404)
    return user


def _busy(message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=503, headers={"Retry-After": "2"})


def _audit_picture(
    request: Request, storage: Any, user: dict[str, Any], action: str, image_sha256: str
) -> None:
    from turnstone.core.audit import record_audit

    audit_uid, ip = audit_context(request)
    record_audit(
        storage,
        audit_uid,
        action,
        "user",
        user["user_id"],
        {"username": user.get("username", ""), "picture": picture_version(image_sha256)},
        ip,
    )


async def get_user_avatar_picture(request: Request) -> Response:
    """GET /v1/api/users/{user_id}/avatar: the uploaded picture, or 404."""
    storage, err = require_storage_or_503(request)
    if err:
        return err
    found = await asyncio.to_thread(storage.get_user_avatar_image, request.path_params["user_id"])
    if found is None:
        return JSONResponse({"error": "No picture for this user"}, status_code=404)
    png, image_sha256 = found
    etag = f'"{image_sha256}"'
    versioned = request.query_params.get("v") == picture_version(image_sha256)
    headers = {
        **_PICTURE_HEADERS,
        "ETag": etag,
        "Cache-Control": _CACHE_VERSIONED if versioned else _CACHE_REVALIDATE,
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(png, media_type="image/png", headers=headers)


async def set_own_avatar_color(request: Request) -> JSONResponse:
    """PUT /v1/api/users/{user_id}/avatar: store the caller's colour."""
    storage, err = require_storage_or_503(request)
    if err:
        return err
    user = await asyncio.to_thread(_own_account, request, storage)
    if isinstance(user, JSONResponse):
        return user
    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body
    color = body.get("color")
    if not isinstance(color, int) or isinstance(color, bool) or not 0 <= color < PALETTE_SIZE:
        return JSONResponse(
            {"error": f"color must be a whole number from 0 to {PALETTE_SIZE - 1}"},
            status_code=400,
        )

    def store() -> dict[str, Any] | None:
        if not storage.set_user_avatar_color(user["user_id"], color):
            return None
        return descriptor_for_user(storage, user)

    avatar = await asyncio.to_thread(store)
    if avatar is None:
        return JSONResponse({"error": _GONE}, status_code=404)
    return JSONResponse({"avatar": avatar})


async def upload_own_avatar_picture(request: Request) -> JSONResponse:
    """PUT /v1/api/users/{user_id}/avatar/picture: store the caller's picture."""
    storage, err = require_storage_or_503(request)
    if err:
        return err
    user = await asyncio.to_thread(_own_account, request, storage)
    if isinstance(user, JSONResponse):
        return user
    user_id = user["user_id"]
    if user_id in _uploading:
        return _busy("Your last picture is still being processed. Try again in a moment.")
    _uploading.add(user_id)
    try:
        parsed = await read_multipart_file_or_400(
            request, field="file", max_bytes=MAX_PICTURE_BYTES
        )
        if isinstance(parsed, JSONResponse):
            return parsed
        _filename, _content_type, data = parsed
        if not data:
            return JSONResponse({"error": "The file is empty"}, status_code=400)
        if len(_admitted) >= _MAX_UPLOADS_IN_FLIGHT:
            return _busy("Too many pictures are being processed. Try again in a moment.")
        _admitted.add(user_id)
        try:
            png, image_sha256 = await anyio.to_thread.run_sync(
                normalize_picture, data, limiter=_DECODE_LIMITER
            )
        except PictureRejectedError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
        finally:
            _admitted.discard(user_id)
    finally:
        _uploading.discard(user_id)

    def store() -> dict[str, Any] | None:
        # The account may have been deleted while the picture decoded.
        if not storage.set_user_avatar_image(user_id, png, image_sha256):
            return None
        _audit_picture(request, storage, user, "user.avatar.set_picture", image_sha256)
        return descriptor_for_user(storage, user)

    avatar = await asyncio.to_thread(store)
    if avatar is None:
        return JSONResponse({"error": _GONE}, status_code=404)
    return JSONResponse({"avatar": avatar})


async def delete_own_avatar_picture(request: Request) -> JSONResponse:
    """DELETE /v1/api/users/{user_id}/avatar/picture: remove the caller's picture."""
    storage, err = require_storage_or_503(request)
    if err:
        return err
    user = await asyncio.to_thread(_own_account, request, storage)
    if isinstance(user, JSONResponse):
        return user

    def clear() -> dict[str, Any]:
        removed = storage.clear_user_avatar_image(user["user_id"])
        if removed:
            _audit_picture(request, storage, user, "user.avatar.remove_picture", removed)
        return descriptor_for_user(storage, user)

    return JSONResponse({"avatar": await asyncio.to_thread(clear)})
