import pytest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch
from models import Client
import docker.errors


def _client(project="acme"):
    c = Client()
    c.docker_project = project
    c.renewal_date = date.today()
    c.created_at = datetime.now(timezone.utc)
    return c


@pytest.mark.asyncio
async def test_stop_client_stops_both_containers():
    from docker_manager import stop_client

    mock_backend = MagicMock()
    mock_openwa = MagicMock()

    def fake_get(name):
        return {"acme-backend-1": mock_backend, "acme-openwa-1": mock_openwa}[name]

    with patch("docker_manager._docker_client") as mock_docker:
        mock_docker.containers.get.side_effect = fake_get
        await stop_client(_client("acme"))

    mock_backend.stop.assert_called_once()
    mock_openwa.stop.assert_called_once()


@pytest.mark.asyncio
async def test_start_client_starts_both_containers():
    from docker_manager import start_client

    mock_backend = MagicMock()
    mock_openwa = MagicMock()

    def fake_get(name):
        return {"acme-backend-1": mock_backend, "acme-openwa-1": mock_openwa}[name]

    with patch("docker_manager._docker_client") as mock_docker:
        mock_docker.containers.get.side_effect = fake_get
        await start_client(_client("acme"))

    mock_backend.start.assert_called_once()
    mock_openwa.start.assert_called_once()


@pytest.mark.asyncio
async def test_stop_client_skips_missing_container():
    from docker_manager import stop_client

    with patch("docker_manager._docker_client") as mock_docker:
        mock_docker.containers.get.side_effect = docker.errors.NotFound("not found")
        # Must not raise
        await stop_client(_client("missing"))


@pytest.mark.asyncio
async def test_stop_client_skips_when_no_docker_project():
    from docker_manager import stop_client
    c = _client()
    c.docker_project = None
    with patch("docker_manager._docker_client") as mock_docker:
        await stop_client(c)
    mock_docker.containers.get.assert_not_called()
