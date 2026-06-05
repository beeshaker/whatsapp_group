import asyncio
import logging
import mimetypes
import os
import uuid
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

MEDIA_DIR = os.getenv("MEDIA_DIR", "/app/media")

_MIMETYPE_EXT_FIXES = {
    "image/jpeg": ".jpg",
    "image/tiff": ".tiff",
}


async def download_media(url: str, dest_dir: str = MEDIA_DIR) -> tuple[str, str, str]:
    """Download media from url, save to dest_dir. Returns (filename, mimetype, file_path)."""
    Path(dest_dir).mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "application/octet-stream")
        mimetype = content_type.split(";")[0].strip()
        ext = _MIMETYPE_EXT_FIXES.get(mimetype) or mimetypes.guess_extension(mimetype) or ".bin"

        filename = f"{uuid.uuid4().hex}{ext}"
        file_path = os.path.join(dest_dir, filename)
        data = response.content

    await asyncio.to_thread(_write_file, file_path, data)
    return filename, mimetype, file_path


def _write_file(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)
