import logging
from models import Client

logger = logging.getLogger(__name__)


async def stop_client(client: Client) -> None:
    logger.info("STUB stop_client: %s", client.docker_project)


async def start_client(client: Client) -> None:
    logger.info("STUB start_client: %s", client.docker_project)
