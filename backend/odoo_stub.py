import logging
from models import Incident

logger = logging.getLogger(__name__)


async def push_incident(incident: Incident) -> None:
    logger.info(
        "TODO: push to Odoo — id=%s property=%s category=%s priority=%s",
        incident.id,
        incident.property_name,
        incident.category,
        incident.priority,
    )
