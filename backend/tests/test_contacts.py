from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

import contacts
import main
import whatsapp
from models import AuditLog, Contact, Incident, IncidentUpdate
from tests.test_ingest import _INCIDENT_CLASSIFICATION, _VALID_PAYLOAD

PHONE = "254711223344"
LID = "123456789012345"


def _incident(**overrides) -> Incident:
    fields = dict(
        group_id="g1@g.us", property_name="Riders", message_body="brakes gone",
        category="brakes", priority="high", confidence=0.9, status="review",
        received_at=datetime.now(timezone.utc), reporter_name="Unknown", reporter_phone=PHONE,
    )
    fields.update(overrides)
    return Incident(**fields)


async def _names(db, model=Incident) -> list[str]:
    rows = (await db.execute(select(model.reporter_name).order_by(model.id).execution_options(populate_existing=True)))
    return [r[0] for r in rows.all()]


# ── contacts.py ──

def test_looks_like_lid():
    assert contacts.looks_like_lid(LID)
    assert not contacts.looks_like_lid(PHONE)
    assert not contacts.looks_like_lid(None)
    assert not contacts.looks_like_lid("abc")


async def test_learn_fills_only_unknown_rows(db_session):
    db_session.add_all([_incident(), _incident(reporter_name="Jane P"), _incident(reporter_phone="254700000001")])
    await db_session.commit()

    assert await contacts.learn(db_session, PHONE, "Jane")

    assert await _names(db_session) == ["Jane", "Jane P", "Unknown"]
    contact = await db_session.get(Contact, PHONE)
    assert (contact.name, contact.source) == ("Jane", contacts.WHATSAPP)


async def test_learn_is_noop_without_change(db_session):
    assert not await contacts.learn(db_session, PHONE, None)
    assert not await contacts.learn(db_session, None, "Jane")
    assert not await contacts.learn(db_session, PHONE, "Unknown")
    assert await contacts.learn(db_session, PHONE, "Jane")
    assert not await contacts.learn(db_session, PHONE, "Jane")


async def test_learn_never_overwrites_manual(db_session):
    db_session.add(_incident())
    await db_session.commit()
    await contacts.set_manual_name(db_session, PHONE, "Rider John", "admin")
    await db_session.commit()

    assert not await contacts.learn(db_session, PHONE, "jd 😎")

    assert (await db_session.get(Contact, PHONE)).name == "Rider John"
    assert await _names(db_session) == ["Rider John"]


async def test_manual_name_relabels_every_row(db_session):
    db_session.add_all([_incident(), _incident(reporter_name="old pushname")])
    await db_session.flush()
    first = (await db_session.execute(select(Incident.id).order_by(Incident.id))).scalars().first()
    db_session.add(IncidentUpdate(incident_id=first, message_body="still broken", reporter_phone=PHONE,
                                  reporter_name="Unknown", received_at=datetime.now(timezone.utc)))
    await db_session.commit()

    renamed = await contacts.set_manual_name(db_session, PHONE, "Rider John", "admin")
    await db_session.commit()

    assert renamed == 3
    assert await _names(db_session) == ["Rider John", "Rider John"]
    assert await _names(db_session, IncidentUpdate) == ["Rider John"]


async def test_clearing_manual_name_removes_contact(db_session):
    await contacts.set_manual_name(db_session, PHONE, "Rider John", "admin")
    await db_session.commit()
    await contacts.set_manual_name(db_session, PHONE, "  ", "admin")
    await db_session.commit()
    assert await db_session.get(Contact, PHONE, populate_existing=True) is None


async def test_resolve_reporter_name_precedence(db_session):
    assert await contacts.resolve_reporter_name(db_session, PHONE, None) == "Unknown"
    assert await contacts.resolve_reporter_name(db_session, PHONE, "Jane") == "Jane"
    await contacts.learn(db_session, PHONE, "Jane")
    assert await contacts.resolve_reporter_name(db_session, PHONE, None) == "Jane"
    assert await contacts.resolve_reporter_name(db_session, PHONE, "New Push") == "New Push"
    await contacts.set_manual_name(db_session, PHONE, "Rider John", "admin")
    await db_session.commit()
    assert await contacts.resolve_reporter_name(db_session, PHONE, "New Push") == "Rider John"


# ── ingest ──

async def _ingest(client, **data_overrides):
    payload = {**_VALID_PAYLOAD, "data": {**_VALID_PAYLOAD["data"], **data_overrides}}
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASSIFICATION)), \
         patch("main.push_incident", new=AsyncMock()):
        return await client.post("/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"})


async def test_ingest_uses_known_name_when_notify_name_missing(client, db_session):
    await contacts.learn(db_session, PHONE, "Jane")
    r = await _ingest(client, id="m-no-name", notifyName=None, author=f"{PHONE}@c.us")
    assert r.status_code == 202
    assert await _names(db_session) == ["Jane"]


async def test_ingest_learns_name_and_fills_older_unknowns(client, db_session):
    db_session.add(_incident())
    await db_session.commit()
    await _ingest(client, id="m-named", notifyName="Jane", author=f"{PHONE}@c.us",
                  body="Front tyre is flat on the bike")
    assert set(await _names(db_session)) == {"Jane"}


async def test_ingest_prefers_manual_name(client, db_session):
    await contacts.set_manual_name(db_session, PHONE, "Rider John", "admin")
    await db_session.commit()
    await _ingest(client, id="m-manual", notifyName="jd 😎", author=f"{PHONE}@c.us")
    assert await _names(db_session) == ["Rider John"]


# ── Contacts page / API ──

async def test_contacts_api_lists_senders(authenticated_client, db_session):
    db_session.add_all([
        _incident(reporter_name="Jane"),
        _incident(reporter_phone=LID),
        _incident(reporter_phone=None),
    ])
    await db_session.commit()

    rows = (await authenticated_client.get("/api/contacts")).json()

    assert [r["phone"] for r in rows] == [LID, PHONE]   # unnamed first
    assert rows[0] == {**rows[0], "name": None, "unresolved_id": True, "messages": 1}
    assert rows[1] == {**rows[1], "name": "Jane", "source": "whatsapp", "unresolved_id": False}


async def test_contacts_api_set_name(authenticated_client, db_session):
    db_session.add(_incident())
    await db_session.commit()

    r = await authenticated_client.post(f"/api/contacts/{PHONE}", json={"name": "Rider John"})

    assert r.status_code == 200
    assert r.json()["renamed_rows"] == 1
    assert await _names(db_session) == ["Rider John"]
    audit = (await db_session.execute(select(AuditLog).where(AuditLog.action == "contact_name"))).scalar_one()
    assert audit.username == "testadmin"


async def test_contacts_api_rejects_bad_phone(authenticated_client):
    r = await authenticated_client.post("/api/contacts/abc", json={"name": "x"})
    assert r.status_code == 422


async def test_contacts_require_admin():
    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as anon:
        for method, url in (("get", "/api/contacts"), ("post", f"/api/contacts/{PHONE}"),
                            ("post", "/api/contacts/sync"), ("get", "/contacts")):
            kwargs = {"json": {"name": "x"}} if method == "post" else {}
            r = await getattr(anon, method)(url, follow_redirects=False, **kwargs)
            assert r.status_code in (302, 401, 403)


async def test_contacts_page_renders(authenticated_client):
    r = await authenticated_client.get("/contacts")
    assert r.status_code == 200
    assert "Sync names from WhatsApp" in r.text


async def test_contacts_sync(authenticated_client, db_session, monkeypatch):
    db_session.add(_incident())
    await db_session.commit()
    monkeypatch.setattr(main, "list_whatsapp_contacts", AsyncMock(return_value=[
        {"id": f"{PHONE}@c.us", "number": PHONE, "name": "Jane Rider", "pushName": "jd"},
        {"id": "254700000009@c.us", "number": "254700000009", "pushName": None},
    ]))

    r = await authenticated_client.post("/api/contacts/sync")

    assert r.json() == {"contacts": 2, "learned": 1}
    assert await _names(db_session) == ["Jane Rider"]


async def test_contacts_sync_whatsapp_down(authenticated_client, monkeypatch):
    monkeypatch.setattr(main, "list_whatsapp_contacts", AsyncMock(return_value=None))
    assert (await authenticated_client.post("/api/contacts/sync")).status_code == 502


# ── scripts/backfill_reporter_identity.py ──

@pytest.fixture
def backfill(monkeypatch):
    from scripts import backfill_reporter_identity as script
    from tests.conftest import _TestSession
    monkeypatch.setattr(script, "AsyncSessionLocal", _TestSession)
    monkeypatch.setattr(whatsapp, "list_contacts", AsyncMock(return_value=[
        {"id": "254700000002@c.us", "number": "254700000002", "name": "Bob Rider"},
    ]))

    def history(messages):
        monkeypatch.setattr(script, "fetch_history", AsyncMock(return_value=messages))
    return script, history


def _hist(msg_id, author, name=""):
    return {"id": msg_id, "isGroup": True, "chatId": "g1@g.us", "author": author, "notifyName": name}


async def test_backfill_dry_run_writes_nothing(backfill, db_session):
    script, history = backfill
    db_session.add(_incident(reporter_phone=LID, message_id="m1"))
    await db_session.commit()
    history([_hist("m1", f"{PHONE}@c.us", "Jane")])

    summary = await script.run(apply=False)

    assert summary["rows_fixed_from_history"] == 1
    row = (await db_session.execute(select(Incident).execution_options(populate_existing=True))).scalar_one()
    assert (row.reporter_phone, row.reporter_name) == (LID, "Unknown")


async def test_backfill_apply_fixes_lid_and_names(backfill, db_session):
    script, history = backfill
    db_session.add_all([
        _incident(reporter_phone=LID, message_id="m1"),
        _incident(reporter_phone="254700000002", message_id="m2"),
        _incident(reporter_phone=PHONE, reporter_name="Kept Name", message_id="m3"),
    ])
    await db_session.commit()
    history([
        _hist("m1", f"{PHONE}@c.us", "Jane"),
        _hist("m2", "254700000002@c.us"),
        _hist("m3", f"{PHONE}@c.us", "Jane"),
        _hist("m4", f"{LID}@lid", "Still hidden"),
    ])

    summary = await script.run(apply=True)

    rows = (await db_session.execute(
        select(Incident.reporter_phone, Incident.reporter_name).order_by(Incident.id)
        .execution_options(populate_existing=True)
    )).all()
    assert rows == [(PHONE, "Jane"), ("254700000002", "Bob Rider"), (PHONE, "Kept Name")]
    assert summary["unknown_after"] == 0
    assert summary["unknown_before"] == 2
    audits = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "reporter_backfill"))).scalars().all()
    assert len(audits) == 1
