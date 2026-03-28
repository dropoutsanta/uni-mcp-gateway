"""Short-lived media token store and HTTP serving.

Tools that produce binary files (images, audio, etc.) create tokens here.
The gateway exposes /media/{token} as a public route so any external tool
(Linear, Notion, etc.) can fetch the file via URL within the TTL window.
"""

import mimetypes
import os
import secrets
import time

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse

_DEFAULT_TTL = 300  # 5 minutes

_tokens: dict[str, tuple[str, float]] = {}  # token -> (file_path, expires_at)


def create_token(file_path: str, ttl: int = _DEFAULT_TTL) -> str:
    token = secrets.token_urlsafe(32)
    _tokens[token] = (file_path, time.time() + ttl)
    _cleanup()
    return token


def _cleanup():
    now = time.time()
    expired = [k for k, (_, exp) in _tokens.items() if exp < now]
    for k in expired:
        del _tokens[k]


async def serve_media(request: Request):
    token = request.path_params.get("token", "")
    entry = _tokens.get(token)
    if not entry:
        return JSONResponse({"error": "Invalid or expired media token"}, status_code=404)

    file_path, expires_at = entry
    if time.time() > expires_at:
        del _tokens[token]
        return JSONResponse({"error": "Media token expired"}, status_code=410)

    if not os.path.isfile(file_path):
        return JSONResponse({"error": "Media file not found"}, status_code=404)

    content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    return FileResponse(file_path, media_type=content_type)
