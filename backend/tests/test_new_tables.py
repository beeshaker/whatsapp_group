from models import AdminProfile, AdminGroupSubscription, ChatSession


def test_admin_profile_columns():
    cols = {c.name for c in AdminProfile.__table__.columns}
    assert cols == {"user_id", "whatsapp_phone"}


def test_admin_group_subscription_columns():
    cols = {c.name for c in AdminGroupSubscription.__table__.columns}
    assert cols == {"id", "user_id", "group_id"}


def test_chat_session_columns():
    cols = {c.name for c in ChatSession.__table__.columns}
    assert cols == {"id", "session_key", "messages", "updated_at"}
