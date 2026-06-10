from database import Base


def test_user_model_columns():
    cols = {c.name for c in Base.metadata.tables["users"].columns}
    assert cols == {"id", "username", "hashed_password", "created_at", "created_by"}


def test_audit_log_model_columns():
    cols = {c.name for c in Base.metadata.tables["audit_log"].columns}
    assert cols == {"id", "username", "action", "incident_id", "detail", "created_at"}


def test_incident_status_history_has_changed_by():
    cols = {c.name for c in Base.metadata.tables["incident_status_history"].columns}
    assert "changed_by" in cols
