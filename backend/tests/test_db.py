from sqlalchemy import text
from models import Incident


async def test_incidents_table_exists(db_session):
    result = await db_session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
    tables = [row[0] for row in result.fetchall()]
    assert "incidents" in tables


async def test_incident_model_columns():
    cols = {c.name for c in Incident.__table__.columns}
    assert cols == {
        "id", "group_id", "property_name", "reporter_name", "reporter_phone",
        "message_body", "category", "severity", "confidence", "status", "received_at",
        "message_id",
    }
