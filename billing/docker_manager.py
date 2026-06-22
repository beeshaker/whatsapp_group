import asyncio
import logging

import docker
import docker.errors

from models import Client

logger = logging.getLogger(__name__)

_docker_client = docker.from_env()


async def _run(fn):
    return await asyncio.get_event_loop().run_in_executor(None, fn)


async def stop_client(client: Client) -> None:
    if not client.docker_project:
        return
    for suffix in ("backend-1", "openwa-1"):
        name = f"{client.docker_project}-{suffix}"
        try:
            container = await _run(lambda n=name: _docker_client.containers.get(n))
            await _run(container.stop)
            logger.info("Stopped %s", name)
        except docker.errors.NotFound:
            logger.warning("Container %s not found", name)
        except Exception:
            logger.exception("Error stopping %s", name)


async def start_client(client: Client) -> None:
    if not client.docker_project:
        return
    for suffix in ("backend-1", "openwa-1"):
        name = f"{client.docker_project}-{suffix}"
        try:
            container = await _run(lambda n=name: _docker_client.containers.get(n))
            await _run(container.start)
            logger.info("Started %s", name)
        except docker.errors.NotFound:
            logger.warning("Container %s not found", name)
        except Exception:
            logger.exception("Error starting %s", name)
