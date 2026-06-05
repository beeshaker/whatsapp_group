import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from media import download_media


async def test_download_media_saves_file_and_returns_metadata():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.headers = {"content-type": "image/jpeg"}
    mock_resp.content = b"\xff\xd8\xff"  # minimal JPEG header bytes

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("media.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            filename, mimetype, file_path = await download_media("http://fake/media/abc", tmpdir)

        assert mimetype == "image/jpeg"
        assert filename.endswith(".jpg")
        assert file_path == os.path.join(tmpdir, filename)
        assert os.path.exists(file_path)
        with open(file_path, "rb") as f:
            assert f.read() == b"\xff\xd8\xff"


async def test_download_media_handles_video():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.headers = {"content-type": "video/mp4"}
    mock_resp.content = b"fakevideodata"

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("media.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            filename, mimetype, file_path = await download_media("http://fake/media/vid", tmpdir)

        assert mimetype == "video/mp4"
        assert filename.endswith(".mp4")


async def test_download_media_raises_on_http_error():
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("media.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                side_effect=Exception("connection refused")
            )
            with pytest.raises(Exception, match="connection refused"):
                await download_media("http://fake/media/bad", tmpdir)


async def test_download_media_creates_dest_dir_if_missing():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.headers = {"content-type": "image/png"}
    mock_resp.content = b"pngdata"

    with tempfile.TemporaryDirectory() as tmpdir:
        new_dir = os.path.join(tmpdir, "subdir", "media")
        with patch("media.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            filename, mimetype, file_path = await download_media("http://fake/media/img", new_dir)

        assert os.path.exists(new_dir)
        assert os.path.exists(file_path)
