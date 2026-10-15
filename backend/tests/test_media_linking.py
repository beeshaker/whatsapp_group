"""How WhatsApp photos get linked to tickets, using the payload shape OpenWA
really sends: caption in `body`, the file base64 in `media.data`, and a
quote-reply's target in `quotedMessage.id`."""
import base64
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

import main as main_module
from models import Incident, IncidentMedia, IncidentUpdate

GROUP = "120363999@g.us"
RIDER = "254700000001"
MECHANIC = "254700000002"
T0 = 1782300000
JPEG = b"\xff\xd8\xff\xe0fakejpeg"

_ISSUE = {"issues": [{
    "category": "brakes", "priority": "high", "confidence": 0.92,
    "message_snippet": "KMGQ 372A brake pads worn",
}]}
_NOISE = {"issues": []}


@pytest.fixture(autouse=True)
def _media_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "MEDIA_DIR", str(tmp_path))
    return tmp_path


def _msg(message_id, *, ts, author=RIDER, msg_type="chat", body="", media=JPEG, quoted=None):
    data = {
        "id": message_id, "type": msg_type, "isGroup": True,
        "chatId": GROUP, "from": GROUP, "chatName": "Pixiilive Riders",
        "author": f"{author}@c.us", "notifyName": "Someone",
        "body": body, "timestamp": ts,
    }
    if msg_type != "chat" and media is not None:
        data["media"] = {"mimetype": "image/jpeg", "data": base64.b64encode(media).decode()}
    if quoted:
        data["quotedMessage"] = {"id": quoted, "body": ""}
    return {"event": "message.received", "data": data}


def _photo(message_id, *, ts, **kw):
    return _msg(message_id, ts=ts, msg_type="image", **kw)


async def _post(client, payload, classification=_NOISE):
    with patch("main.classify_message", new=AsyncMock(return_value=classification)), \
         patch("main.classify_update_or_new", new=AsyncMock(return_value={"routing": "new"})), \
         patch("main.push_incident", new=AsyncMock()):
        r = await client.post("/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"})
    assert r.status_code == 202
    return r.json()


async def _media(db_session):
    db_session.expire_all()
    return (await db_session.execute(select(IncidentMedia).order_by(IncidentMedia.id))).scalars().all()


async def _ticket(client, message_id="txt-1", ts=T0, author=RIDER):
    res = await _post(client, _msg(message_id, ts=ts, author=author, body="KMGQ 372A brake pads worn"), _ISSUE)
    assert res["tickets_created"] == 1
    return (await client.get("/incidents")).json()[0]["id"]


async def test_captioned_photo_creates_ticket_and_saves_the_inline_file(client, db_session, _media_dir):
    res = await _post(client, _photo("img-1", ts=T0, body="KMGQ 372A brake pads worn"), _ISSUE)

    assert res["status"] == "staged_media"
    assert res["link_method"] == "caption"
    [m] = await _media(db_session)
    assert m.incident_id == res["incident_id"]
    assert m.caption == "KMGQ 372A brake pads worn"
    assert m.mimetype == "image/jpeg"
    assert os.path.dirname(m.file_path) == str(_media_dir)
    with open(m.file_path, "rb") as f:
        assert f.read() == JPEG


async def test_text_then_photo_from_same_sender_links_by_proximity(client, db_session):
    incident_id = await _ticket(client)

    res = await _post(client, _photo("img-1", ts=T0 + 90))

    assert res["incident_id"] == incident_id
    assert res["link_method"] == "proximity"


async def test_uncaptioned_photo_from_another_sender_is_not_guessed_onto_their_ticket(client, db_session):
    await _ticket(client, author=RIDER)

    res = await _post(client, _photo("img-1", ts=T0 + 60, author=MECHANIC))

    assert "incident_id" not in res
    assert res["unassigned"] is True
    [m] = await _media(db_session)
    assert m.incident_id is None
    assert m.reporter_phone == MECHANIC


async def test_photo_outside_the_window_stays_unassigned(client, db_session):
    await _ticket(client)

    res = await _post(client, _photo("img-1", ts=T0 + 3600))

    assert "incident_id" not in res


async def test_photo_then_text_sweeps_the_waiting_photo_onto_the_new_ticket(client, db_session):
    await _post(client, _photo("img-1", ts=T0))
    await _post(client, _photo("img-2", ts=T0 + 5))

    res = await _post(client, _msg("txt-1", ts=T0 + 40, body="KMGQ 372A brake pads worn"), _ISSUE)

    assert res["media_attached"] == 2
    incident_id = (await client.get("/incidents")).json()[0]["id"]
    rows = await _media(db_session)
    assert [m.incident_id for m in rows] == [incident_id, incident_id]
    assert {m.link_method for m in rows} == {"sweep"}


async def test_sweep_ignores_other_senders_photos(client, db_session):
    await _post(client, _photo("img-1", ts=T0, author=MECHANIC))

    res = await _post(client, _msg("txt-1", ts=T0 + 40, body="KMGQ 372A brake pads worn"), _ISSUE)

    assert "media_attached" not in res
    [m] = await _media(db_session)
    assert m.incident_id is None


async def test_album_photos_before_and_after_the_captioned_one_all_follow_it(client, db_session):
    await _post(client, _photo("img-1", ts=T0))
    res = await _post(client, _photo("img-2", ts=T0 + 1, body="KMGQ 372A brake pads worn"), _ISSUE)
    await _post(client, _photo("img-3", ts=T0 + 2))

    incident_id = res["incident_id"]
    rows = await _media(db_session)
    assert sorted(m.message_id for m in rows) == ["img-1", "img-2", "img-3"]
    assert {m.incident_id for m in rows} == {incident_id}


async def test_quote_reply_photo_goes_to_the_quoted_ticket_even_from_another_sender_much_later(client, db_session):
    incident_id = await _ticket(client, message_id="txt-1")

    res = await _post(client, _photo("img-1", ts=T0 + 86400, author=MECHANIC, quoted="txt-1"))

    assert res["incident_id"] == incident_id
    assert res["link_method"] == "quote"


async def test_quote_reply_to_a_bot_reply_goes_to_that_ticket(client, db_session):
    incident_id = await _ticket(client)
    db_session.add(IncidentUpdate(
        incident_id=incident_id, message_id="bot-reply-1", reporter_name="Dashboard",
        message_body="Which bike?", received_at=datetime.fromtimestamp(T0 + 100, tz=timezone.utc),
        ai_linked=False,
    ))
    await db_session.commit()

    res = await _post(client, _photo("img-1", ts=T0 + 86400, author=MECHANIC, quoted="bot-reply-1"))

    assert res["incident_id"] == incident_id
    [m] = await _media(db_session)
    assert m.update_id is not None


async def test_text_quoting_a_waiting_photo_attaches_it(client, db_session):
    await _post(client, _photo("img-1", ts=T0, author=MECHANIC))

    await _post(
        client,
        _msg("txt-1", ts=T0 + 7200, author=RIDER, body="KMGQ 372A brake pads worn", quoted="img-1"),
        _ISSUE,
    )

    incident_id = (await client.get("/incidents")).json()[0]["id"]
    [m] = await _media(db_session)
    assert m.incident_id == incident_id
    assert m.link_method == "quote"


async def test_redelivered_photo_is_not_saved_twice(client, db_session):
    await _ticket(client)
    payload = _photo("img-1", ts=T0 + 30)

    await _post(client, payload)
    await _post(client, payload)

    rows = await _media(db_session)
    assert len(rows) == 1


async def test_oversized_inline_media_is_skipped_but_caption_still_makes_a_ticket(client, db_session, monkeypatch):
    import media
    monkeypatch.setattr(media, "MAX_MEDIA_BYTES", 4)

    res = await _post(client, _photo("img-1", ts=T0, body="KMGQ 372A brake pads worn"), _ISSUE)

    assert res["status"] == "staged_media"
    assert len((await client.get("/incidents")).json()) == 1
    assert await _media(db_session) == []


async def test_voice_notes_are_ignored(client, db_session):
    res = await _post(client, _msg("ptt-1", ts=T0, msg_type="audio", media=b"ogg"))

    assert res["status"] == "ignored"
    assert await _media(db_session) == []


async def _plate_ticket(db_session, plate="KMGQ372A"):
    inc = Incident(
        group_id=GROUP, property_name="Pixiilive Riders", reporter_name="Rider",
        reporter_phone=RIDER, message_body=f"{plate} brakes", category="brakes",
        vehicle_plate=plate, priority="high", confidence=0.9, status="review",
        received_at=datetime.fromtimestamp(T0 - 86400, tz=timezone.utc), message_id="old-1",
    )
    db_session.add(inc)
    await db_session.commit()
    return inc.id


async def test_fleet_photo_captioned_only_with_a_plate_goes_to_that_bikes_open_ticket(client, db_session, monkeypatch):
    monkeypatch.setattr(main_module, "FLEET_PLATE_MODE", True)
    incident_id = await _plate_ticket(db_session)

    res = await _post(client, _photo("img-1", ts=T0, author=MECHANIC, body="KMGQ 372A"))

    assert res["incident_id"] == incident_id
    assert res["link_method"] == "plate"


async def test_fleet_photos_then_plate_only_text_attach_to_that_bike(client, db_session, monkeypatch):
    monkeypatch.setattr(main_module, "FLEET_PLATE_MODE", True)
    incident_id = await _plate_ticket(db_session)
    await _post(client, _photo("img-1", ts=T0, author=MECHANIC))

    res = await _post(client, _msg("txt-1", ts=T0 + 30, author=MECHANIC, body="kmgq372a"))

    assert res["status"] == "noise"
    [m] = await _media(db_session)
    assert m.incident_id == incident_id


# --- Dashboard: unassigned list, attach/move, dismiss, upload ---

async def test_unassigned_list_then_manual_attach(client, db_session):
    incident_id = await _ticket(client)
    await _post(client, _photo("img-1", ts=T0 + 60, author=MECHANIC, body="look"))

    [item] = (await client.get("/media/unassigned")).json()
    assert item["caption"] == "look"
    assert item["reporter_phone"] == MECHANIC

    r = await client.patch(f"/media/{item['id']}", json={"incident_id": incident_id})
    assert r.status_code == 200
    assert r.json()["link_method"] == "manual"
    assert (await client.get("/media/unassigned")).json() == []
    detail = (await client.get(f"/incidents/{incident_id}")).json()
    assert [m["id"] for m in detail["media"]] == [item["id"]]


async def test_logged_in_user_can_view_media_without_api_key(client, db_session):
    res = await _post(client, _photo("img-1", ts=T0, body="KMGQ 372A brake pads worn"), _ISSUE)
    [m] = await _media(db_session)

    r = await client.get(f"/media/{m.id}")

    assert r.status_code == 200
    assert r.content == JPEG
    assert r.headers["content-disposition"].startswith("inline")


async def test_delete_keeps_file_another_ticket_still_uses(client, db_session):
    async def _two(caption, db):
        return {"issues": [
            {"category": "brakes", "priority": "high", "confidence": 0.9, "message_snippet": "KMGQ 372A brakes"},
            {"category": "tyres", "priority": "high", "confidence": 0.85, "message_snippet": "KMGR 111B tyre"},
        ]}
    with patch("main.classify_message", new=_two), \
         patch("main.classify_update_or_new", new=AsyncMock(return_value={"routing": "new"})), \
         patch("main.push_incident", new=AsyncMock()):
        await client.post("/api/v1/ops/ingest", json=_photo("img-1", ts=T0, body="two bikes"),
                          headers={"X-API-Key": "test-secret"})
    first, second = await _media(db_session)
    assert first.file_path == second.file_path

    assert (await client.delete(f"/media/{first.id}")).status_code == 200
    assert os.path.exists(second.file_path)
    assert (await client.delete(f"/media/{second.id}")).status_code == 200
    assert not os.path.exists(second.file_path)


async def test_staff_upload_attaches_to_ticket(client, db_session):
    incident_id = await _ticket(client)

    r = await client.post(
        f"/incidents/{incident_id}/media", files={"file": ("bike.jpg", JPEG, "image/jpeg")},
    )

    assert r.status_code == 200
    assert r.json()["link_method"] == "upload"
    [m] = await _media(db_session)
    assert m.incident_id == incident_id


async def test_staff_upload_rejects_other_file_types(client, db_session):
    incident_id = await _ticket(client)

    r = await client.post(
        f"/incidents/{incident_id}/media", files={"file": ("x.zip", b"PK", "application/zip")},
    )

    assert r.status_code == 422
