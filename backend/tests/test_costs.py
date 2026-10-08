import os
from datetime import date, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

import main as backend_main
import reports
from models import AuditLog, IncidentCost
from tests.test_reports import (  # noqa: F401  (fixtures)
    NAIROBI,
    _make_client,
    _parse_csv,
    _restore_main_module_state,
    fleet_admin,
    fleet_g1_user,
    non_fleet_admin,
    seeded,
)

PDF = b"%PDF-1.4 fake receipt"


@pytest.fixture
def media_dir(tmp_path, monkeypatch):
    # main is reloaded by the client fixtures; patch the reloaded module.
    monkeypatch.setattr(backend_main, "MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr(backend_main, "RECEIPTS_DIR", str(tmp_path / "receipts"))
    return tmp_path


@pytest_asyncio.fixture
async def fleet_user(monkeypatch):
    async with await _make_client(monkeypatch, "rider", "user", groups=["g1@g.us", "g2@g.us"]) as c:
        yield c
    backend_main.app.dependency_overrides.clear()


async def _add(client, incident_id, amount="1500", cost_type="parts", day=None, receipt=None, **extra):
    data = {"amount": amount, "cost_type": cost_type,
            "incurred_on": (day or date.today()).isoformat(), **extra}
    files = {"receipt": receipt} if receipt else None
    return await client.post(f"/incidents/{incident_id}/costs", data=data, files=files)


async def test_add_list_and_total(seeded, fleet_admin, media_dir):
    a = seeded["a"][0]
    r = await _add(fleet_admin, a, "1,500", "parts", vendor="Mama Garage", description="brake pads",
                   receipt=("pads.pdf", PDF, "application/pdf"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["amount"] == 1500.0
    assert body["vendor"] == "Mama Garage"
    assert body["receipt_url"] == f"/costs/{body['id']}/receipt"
    assert (await _add(fleet_admin, a, "350.50", "labour")).status_code == 201

    listing = (await fleet_admin.get(f"/incidents/{a}/costs")).json()
    assert listing["cost_total"] == 1850.5
    assert [c["cost_type"] for c in listing["costs"]] == ["parts", "labour"]

    detail = (await fleet_admin.get(f"/incidents/{a}")).json()
    assert detail["cost_total"] == 1850.5

    receipt = await fleet_admin.get(body["receipt_url"])
    assert receipt.status_code == 200
    assert receipt.content == PDF
    assert receipt.headers["content-type"] == "application/pdf"


async def test_add_writes_audit_log(seeded, fleet_admin, db_session, media_dir):
    a = seeded["a"][0]
    await _add(fleet_admin, a, "2000", "towing")
    audit = (await db_session.execute(select(AuditLog).where(AuditLog.action == "cost_add"))).scalar_one()
    assert audit.incident_id == a
    assert "2,000.00" in audit.detail


@pytest.mark.parametrize("amount, cost_type, day, status", [
    ("abc", "parts", None, 422),
    ("0", "parts", None, 422),
    ("-5", "parts", None, 422),
    ("100", "bribe", None, 422),
    ("100", "parts", "not-a-date", 422),
])
async def test_add_validation(seeded, fleet_admin, media_dir, amount, cost_type, day, status):
    data = {"amount": amount, "cost_type": cost_type, "incurred_on": day or date.today().isoformat()}
    r = await fleet_admin.post(f"/incidents/{seeded['a'][0]}/costs", data=data)
    assert r.status_code == status


async def test_receipt_must_be_image_or_pdf(seeded, fleet_admin, media_dir):
    r = await _add(fleet_admin, seeded["a"][0], receipt=("x.exe", b"MZ", "application/octet-stream"))
    assert r.status_code == 422


async def test_receipt_size_limit(seeded, fleet_admin, media_dir, monkeypatch):
    monkeypatch.setattr(backend_main, "_RECEIPT_MAX_BYTES", 10)
    r = await _add(fleet_admin, seeded["a"][0], receipt=("big.pdf", PDF, "application/pdf"))
    assert r.status_code == 413


async def test_delete_removes_receipt_file(seeded, fleet_admin, media_dir):
    a = seeded["a"][0]
    cost = (await _add(fleet_admin, a, receipt=("r.jpg", b"\xff\xd8jpeg", "image/jpeg"))).json()
    files = list((media_dir / "receipts").iterdir())
    assert len(files) == 1

    r = await fleet_admin.post(f"/incidents/{a}/costs/{cost['id']}/delete")
    assert r.status_code == 200
    assert list((media_dir / "receipts").iterdir()) == []
    assert (await fleet_admin.get(f"/incidents/{a}/costs")).json()["costs"] == []


async def test_delete_wrong_incident_404(seeded, fleet_admin, media_dir):
    cost = (await _add(fleet_admin, seeded["a"][0])).json()
    r = await fleet_admin.post(f"/incidents/{seeded['a'][1]}/costs/{cost['id']}/delete")
    assert r.status_code == 404


async def test_receipt_path_outside_media_dir_forbidden(seeded, fleet_admin, db_session, media_dir, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "secret.pdf"
    outside.write_bytes(PDF)
    cost = IncidentCost(
        incident_id=seeded["a"][0], amount=1, cost_type="other", incurred_on=date.today(),
        receipt_path=str(outside), receipt_mimetype="application/pdf", created_by="x",
        created_at=backend_main.datetime.now(backend_main.timezone.utc),
    )
    db_session.add(cost)
    await db_session.commit()
    assert (await fleet_admin.get(f"/costs/{cost.id}/receipt")).status_code == 403


async def test_regular_user_can_view_but_not_add(seeded, fleet_admin, fleet_user, media_dir, monkeypatch):
    a = seeded["a"][0]
    cost = (await _add(fleet_admin, a, receipt=("r.pdf", PDF, "application/pdf"))).json()

    # Undo the test fixture's require_admin bypass so the real role check runs.
    from auth import require_admin
    backend_main.app.dependency_overrides.pop(require_admin, None)

    assert (await fleet_user.get(f"/incidents/{a}/costs")).status_code == 200
    assert (await fleet_user.get(cost["receipt_url"])).status_code == 200
    r = await _add(fleet_user, a)
    assert r.status_code in (302, 403)
    r = await fleet_user.post(f"/incidents/{a}/costs/{cost['id']}/delete")
    assert r.status_code in (302, 403)


async def test_receipt_hidden_outside_user_groups(seeded, fleet_admin, fleet_g1_user, media_dir):
    cost = (await _add(fleet_admin, seeded["b"], receipt=("r.pdf", PDF, "application/pdf"))).json()
    assert (await fleet_g1_user.get(cost["receipt_url"])).status_code == 403


async def test_costs_404_when_not_fleet_mode(seeded, non_fleet_admin, media_dir):
    a = seeded["a"][0]
    assert (await non_fleet_admin.get(f"/incidents/{a}/costs")).status_code == 404
    assert (await _add(non_fleet_admin, a)).status_code == 404
    assert "cost_total" not in (await non_fleet_admin.get(f"/incidents/{a}")).json()


# ── reports ──

async def test_plate_table_and_overview_costs_by_range(seeded, fleet_admin, db_session, media_dir):
    a0, a1 = seeded["a"][:2]
    await _add(fleet_admin, a0, "1000", "parts")
    await _add(fleet_admin, a1, "500", "labour")
    await _add(fleet_admin, a1, "9999", "parts", day=date.today() - timedelta(days=100))
    await _add(fleet_admin, seeded["none"], "200", "other")   # unassigned plate

    start, end, first, last = reports.resolve_range(NAIROBI, "30")
    rows = {r["plate"]: r for r in await reports.plate_table(
        db_session, None, start, end, backend_main.datetime.now(backend_main.timezone.utc), first, last)}
    assert rows["KAAA111A"]["cost"] == 1500.0
    assert rows["KBBB222B"]["cost"] == 0.0

    ov = await reports.fleet_overview(db_session, None, start, end, first, last, NAIROBI)
    assert ov["repair_spend"] == 1700.0

    start, end, first, last = reports.resolve_range(NAIROBI, "all", earliest=date.today() - timedelta(days=200))
    rows = {r["plate"]: r for r in await reports.plate_table(
        db_session, None, start, end, backend_main.datetime.now(backend_main.timezone.utc), first, last)}
    assert rows["KAAA111A"]["cost"] == 11499.0


async def test_plate_with_cost_but_no_reports_in_range_is_listed(seeded, fleet_admin, db_session, media_dir):
    # KCCC333C's only ticket is 60 days old, but it was repaired this week.
    await _add(fleet_admin, seeded["c"], "800", "parts")
    start, end, first, last = reports.resolve_range(NAIROBI, "30")
    rows = {r["plate"]: r for r in await reports.plate_table(
        db_session, None, start, end, backend_main.datetime.now(backend_main.timezone.utc), first, last)}
    assert rows["KCCC333C"]["cost"] == 800.0
    assert rows["KCCC333C"]["reports"] == 0


async def test_reports_pages_show_costs(seeded, fleet_admin, media_dir):
    await _add(fleet_admin, seeded["a"][0], "1250", "parts", receipt=("r.pdf", PDF, "application/pdf"))
    page = (await fleet_admin.get("/reports")).text
    assert "Repair spend" in page
    assert "KES 1,250" in page
    plate = (await fleet_admin.get("/reports/plate/KAAA111A")).text
    assert "KES 1,250" in plate
    assert "/receipt" in plate


async def test_ticket_export_has_repair_cost(seeded, fleet_admin, media_dir):
    await _add(fleet_admin, seeded["a"][0], "1000", "parts")
    await _add(fleet_admin, seeded["a"][0], "250", "labour")
    rows = _parse_csv((await fleet_admin.get("/reports/export.csv?days=30")).text)
    by_id = {int(r[0]): dict(zip(rows[0], r)) for r in rows[1:]}
    assert by_id[seeded["a"][0]]["repair_cost"] == "1250.00"
    assert by_id[seeded["a"][1]]["repair_cost"] == ""


async def test_costs_csv(seeded, fleet_admin, fleet_g1_user, media_dir):
    await _add(fleet_admin, seeded["a"][0], "1000", "parts", vendor="=evil()",
               receipt=("r.pdf", PDF, "application/pdf"))
    await _add(fleet_admin, seeded["b"], "300", "towing")
    r = await fleet_admin.get("/reports/costs.csv?days=30")
    assert r.status_code == 200
    rows = _parse_csv(r.text)
    assert rows[0] == reports.COST_EXPORT_COLUMNS
    lines = [dict(zip(rows[0], row)) for row in rows[1:]]
    assert {(l["vehicle_plate"], l["amount"]) for l in lines} == {("KAAA111A", "1000.00"), ("KBBB222B", "300.00")}
    parts = next(l for l in lines if l["cost_type"] == "parts")
    assert parts["vendor"].startswith("'=")
    assert parts["receipt"] == "yes"

    plate_rows = _parse_csv((await fleet_admin.get("/reports/costs.csv?plate=kbbb222b")).text)
    assert [r[2] for r in plate_rows[1:]] == ["KBBB222B"]

    scoped = _parse_csv((await fleet_g1_user.get("/reports/costs.csv")).text)
    assert [r[2] for r in scoped[1:]] == ["KAAA111A"]
