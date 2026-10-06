import csv
import importlib
import io
import zoneinfo
from datetime import datetime, timedelta, timezone
from html import unescape

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import main as backend_main
import reports
from database import get_db
from models import Incident, IncidentStatusHistory, IncidentUpdate, User, UserGroup

NAIROBI = zoneinfo.ZoneInfo("Africa/Nairobi")


@pytest.fixture(autouse=True)
def _restore_main_module_state():
    yield
    importlib.reload(backend_main)


def _ago(**kw) -> datetime:
    return datetime.now(timezone.utc) - timedelta(**kw)


def _incident(**overrides) -> Incident:
    fields = dict(
        group_id="g1@g.us", property_name="Pixiilive Riders", message_body="issue",
        category="brakes", priority="high", confidence=0.9, status="review",
        received_at=_ago(days=1), reporter_name="Rider",
    )
    fields.update(overrides)
    return Incident(**fields)


@pytest_asyncio.fixture
async def seeded():
    """KAAA111A: 3 open tickets this week (a repeat vehicle).
    KBBB222B: 1 ticket resolved yesterday, in group g2.
    KCCC333C: 1 open ticket 60 days ago (outside the default 30-day range).
    Plus 1 open ticket with no plate."""
    from tests.conftest import _TestSession
    async with _TestSession() as s:
        a = [_incident(vehicle_plate="KAAA111A", received_at=_ago(days=d), message_body=f"A{d}") for d in (1, 3, 5)]
        b = _incident(vehicle_plate="KBBB222B", group_id="g2@g.us", status="resolved",
                      category="tyres", received_at=_ago(days=4), message_body="=HYPERLINK(\"x\")")
        c = _incident(vehicle_plate="KCCC333C", received_at=_ago(days=60))
        none = _incident(vehicle_plate=None, priority="low")
        s.add_all(a + [b, c, none])
        await s.flush()
        s.add(IncidentStatusHistory(incident_id=b.id, from_status="review", to_status="resolved",
                                    changed_at=_ago(days=1), changed_by="fleetadmin"))
        s.add(IncidentUpdate(incident_id=a[0].id, message_body="still broken",
                             received_at=_ago(hours=20), reporter_name="Rider"))
        await s.commit()
        return {"a": [i.id for i in a], "b": b.id, "c": c.id, "none": none.id}


async def _make_client(monkeypatch, username: str, role: str, groups=(), fleet=True):
    monkeypatch.setenv("FLEET_PLATE_MODE", "true" if fleet else "false")
    from tests.conftest import _TestSession
    from auth import require_login, require_admin, hash_password
    importlib.reload(backend_main)

    async def _override_get_db():
        async with _TestSession() as session:
            yield session

    async def _override_user():
        return username

    backend_main.app.dependency_overrides[get_db] = _override_get_db
    backend_main.app.dependency_overrides[require_login] = _override_user
    backend_main.app.dependency_overrides[require_admin] = _override_user
    async with _TestSession() as session:
        user = User(username=username, hashed_password=hash_password("x"),
                    created_at=datetime.now(timezone.utc), role=role)
        session.add(user)
        await session.flush()
        for g in groups:
            session.add(UserGroup(user_id=user.id, group_id=g))
        await session.commit()
    return AsyncClient(transport=ASGITransport(app=backend_main.app), base_url="http://test")


@pytest_asyncio.fixture
async def fleet_admin(monkeypatch):
    async with await _make_client(monkeypatch, "fleetadmin", "admin") as c:
        yield c
    backend_main.app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def fleet_g1_user(monkeypatch):
    async with await _make_client(monkeypatch, "g1user", "user", groups=["g1@g.us"]) as c:
        yield c
    backend_main.app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def non_fleet_admin(monkeypatch):
    async with await _make_client(monkeypatch, "plainadmin", "admin", fleet=False) as c:
        yield c
    backend_main.app.dependency_overrides.clear()


# ── reports.py ──

async def test_fleet_overview_counts(seeded, db_session):
    start, end, first, last = reports.resolve_range(NAIROBI, "30")
    ov = await reports.fleet_overview(db_session, None, start, end, first, last, NAIROBI)
    assert ov["open_count"] == 5          # 3 A + C + unassigned
    assert ov["vehicles_open"] == 2       # A and C
    assert ov["open_unassigned"] == 1
    assert ov["new_in_range"] == 5        # C is 60 days old
    assert ov["resolved_in_range"] == 1
    assert dict(ov["backlog_by_priority"]) == {"urgent": 0, "high": 4, "medium": 0, "low": 1}
    assert ov["trend"]["granularity"] == "day"
    assert len(ov["trend"]["buckets"]) == 30
    assert sum(b["new"] for b in ov["trend"]["buckets"]) == 5
    assert sum(b["resolved"] for b in ov["trend"]["buckets"]) == 1


async def test_trend_switches_to_weekly_over_31_days(seeded, db_session):
    start, end, first, last = reports.resolve_range(NAIROBI, "90")
    ov = await reports.fleet_overview(db_session, None, start, end, first, last, NAIROBI)
    assert ov["trend"]["granularity"] == "week"
    assert len(ov["trend"]["buckets"]) == 13
    assert ov["trend"]["buckets"][-1]["end"] == last.isoformat()
    assert sum(b["new"] for b in ov["trend"]["buckets"]) == 6


async def _plate_rows(db, days, **kw):
    earliest = await reports.earliest_day(db, None, NAIROBI)
    start, end, _, _ = reports.resolve_range(NAIROBI, days, earliest=earliest, **kw)
    return {r["plate"]: r for r in await reports.plate_table(
        db, None, start, end, datetime.now(timezone.utc))}


async def test_plate_table_all_time_is_full_history(seeded, db_session):
    rows = await _plate_rows(db_session, "all")
    assert set(rows) == {"KAAA111A", "KBBB222B", "KCCC333C"}
    assert rows["KAAA111A"] | {"last_reported": None} == {
        "plate": "KAAA111A", "tickets": 3, "reports": 4, "open": 3, "recent": 4,
        "repeat": True, "last_reported": None, "top_category": "brakes",
    }
    assert rows["KBBB222B"]["open"] == 0 and rows["KBBB222B"]["repeat"] is False
    # 60 days old: still listed with its ticket and category, just not recent.
    c = rows["KCCC333C"]
    assert (c["tickets"], c["reports"], c["recent"], c["top_category"]) == (1, 1, 0, "brakes")


async def test_plate_table_follows_selected_range(seeded, db_session):
    rows = await _plate_rows(db_session, "30")
    assert set(rows) == {"KAAA111A", "KBBB222B"}     # C last reported 60 days ago
    rows = await _plate_rows(db_session, None, date_from="2000-01-01", date_to="2000-01-31")
    assert rows == {}


async def test_plate_table_follow_up_only_in_range(seeded, db_session):
    """A plate whose ticket predates the range but got a follow-up inside it
    is listed with 0 tickets, 1 report and its ticket's category."""
    db_session.add(IncidentUpdate(incident_id=seeded["c"], message_body="again",
                                  received_at=_ago(days=2)))
    await db_session.commit()
    c = (await _plate_rows(db_session, "7"))["KCCC333C"]
    assert (c["tickets"], c["reports"], c["open"], c["top_category"]) == (0, 1, 1, "brakes")


async def test_repeat_flag_counts_follow_ups_on_one_ticket(db_session):
    """Fleet routing turns repeat reports of an open bike into follow-ups, so
    one ticket with 2 follow-ups in 30 days is a repeat vehicle."""
    t = _incident(vehicle_plate="KDDD444D", received_at=_ago(days=10))
    db_session.add(t)
    await db_session.flush()
    for d in (6, 2):
        db_session.add(IncidentUpdate(incident_id=t.id, message_body="again", received_at=_ago(days=d)))
    db_session.add(IncidentUpdate(incident_id=t.id, message_body="latest", received_at=_ago(days=1)))
    await db_session.commit()
    [row] = (await _plate_rows(db_session, "30")).values()
    assert (row["tickets"], row["reports"], row["recent"], row["repeat"]) == (1, 4, 4, True)
    assert row["last_reported"] > _ago(days=1, minutes=1)


async def test_all_time_range_starts_at_first_ticket(seeded, db_session):
    earliest = await reports.earliest_day(db_session, None, NAIROBI)
    assert earliest == _ago(days=60).astimezone(NAIROBI).date()
    start, end, first, last = reports.resolve_range(NAIROBI, "all", earliest=earliest)
    assert first == earliest
    ov = await reports.fleet_overview(db_session, None, start, end, first, last, NAIROBI)
    assert ov["new_in_range"] == 6
    assert ov["trend"]["granularity"] == "week"


def test_trend_uses_4_week_buckets_beyond_6_months():
    from datetime import date
    t = reports._trend([], [], date(2026, 1, 1), date(2026, 10, 1), NAIROBI)
    assert t["granularity"] == "4 weeks"
    assert t["buckets"][-1]["end"] == "2026-10-01"


def test_resolve_range_custom_dates_and_fallback():
    _, _, first, last = reports.resolve_range(NAIROBI, None, "2026-09-30", "2026-09-01")
    assert (first.isoformat(), last.isoformat()) == ("2026-09-01", "2026-09-30")
    _, _, first, last = reports.resolve_range(NAIROBI, "45", "bad", "2026-09-01")
    assert (last - first).days == 29     # unsupported days -> 30
    _, _, first, last = reports.resolve_range(NAIROBI, "all", earliest=None)
    assert first == last                 # no tickets yet -> just today


# ── routes ──

async def test_reports_routes_404_when_fleet_mode_off(non_fleet_admin):
    for path in ("/reports", "/reports/export.csv", "/reports/plate/KAAA111A"):
        assert (await non_fleet_admin.get(path)).status_code == 404


async def test_reports_page_renders(seeded, fleet_admin):
    r = await fleet_admin.get("/reports")
    assert r.status_code == 200
    html = r.text
    assert 'id="stat-open">5<' in html
    assert 'id="stat-vehicles-open">2<' in html
    assert "/reports/plate/KAAA111A" in html
    assert "badge-repeat" in html
    assert 'href="/reports"' in html     # nav link shown in fleet mode


async def test_nav_hides_reports_link_outside_fleet_mode(non_fleet_admin):
    r = await non_fleet_admin.get("/summaries")
    assert r.status_code == 200
    assert 'href="/reports"' not in r.text


async def test_reports_page_scoped_to_user_groups(seeded, fleet_g1_user):
    html = (await fleet_g1_user.get("/reports")).text
    assert "KAAA111A" in html
    assert "KBBB222B" not in html        # g2 only


async def test_plate_page_shows_timeline(seeded, fleet_admin):
    r = await fleet_admin.get("/reports/plate/kaaa 111a")
    assert r.status_code == 200
    for ticket_id in seeded["a"]:
        assert f"#{ticket_id}" in r.text
    assert "still broken" in r.text


async def test_plate_page_404s_for_bad_or_unknown_plate(seeded, fleet_admin):
    assert (await fleet_admin.get("/reports/plate/NOTAPLATE")).status_code == 404
    assert (await fleet_admin.get("/reports/plate/KZZZ999Z")).status_code == 404


async def test_plate_page_404s_outside_user_groups(seeded, fleet_g1_user):
    assert (await fleet_g1_user.get("/reports/plate/KBBB222B")).status_code == 404


def _parse_csv(body: str) -> list[list[str]]:
    assert body.startswith("﻿")
    return list(csv.reader(io.StringIO(body[1:])))


async def test_export_csv_columns_and_rows(seeded, fleet_admin):
    r = await fleet_admin.get("/reports/export.csv?days=30")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = _parse_csv(r.text)
    assert rows[0] == reports.EXPORT_COLUMNS
    by_id = {int(row[0]): dict(zip(rows[0], row)) for row in rows[1:]}
    assert set(by_id) == set(seeded["a"]) | {seeded["b"], seeded["none"]}
    b = by_id[seeded["b"]]
    assert b["vehicle_plate"] == "KBBB222B"
    assert b["resolved_at"] != ""
    assert b["message"].startswith("'=")   # formula neutralised
    assert by_id[seeded["a"][0]]["resolved_at"] == ""


async def test_export_csv_filters(seeded, fleet_admin):
    rows = _parse_csv((await fleet_admin.get("/reports/export.csv?plate=kaaa111a")).text)
    assert {int(r[0]) for r in rows[1:]} == set(seeded["a"])
    rows = _parse_csv((await fleet_admin.get("/reports/export.csv?status=resolved")).text)
    assert [int(r[0]) for r in rows[1:]] == [seeded["b"]]
    rows = _parse_csv((await fleet_admin.get("/reports/export.csv?days=90&category=brakes")).text)
    assert seeded["c"] in {int(r[0]) for r in rows[1:]}
    assert (await fleet_admin.get("/reports/export.csv?plate=bogus")).status_code == 422


async def test_export_csv_scoped_to_user_groups(seeded, fleet_g1_user):
    rows = _parse_csv((await fleet_g1_user.get("/reports/export.csv")).text)
    assert seeded["b"] not in {int(r[0]) for r in rows[1:]}


async def test_export_csv_all_time(seeded, fleet_admin):
    rows = _parse_csv((await fleet_admin.get("/reports/export.csv?days=all")).text)
    assert seeded["c"] in {int(r[0]) for r in rows[1:]}
    assert len(rows) == 7                # header + all 6 tickets


async def test_reports_page_all_time_chip(seeded, fleet_admin):
    html = (await fleet_admin.get("/reports?days=all")).text
    assert 'class="range-chip active" href="/reports?days=all"' in html
    assert 'id="stat-new">6<' in html


async def test_reports_page_links_to_filtered_lists(seeded, fleet_admin):
    html = unescape((await fleet_admin.get("/reports")).text)
    open_qs = "status=review&status=new&status=acknowledged"
    assert f'href="/?{open_qs}"' in html                       # open tickets
    assert 'href="/reports?days=all&open=1#vehicles"' in html  # vehicles with open issues
    assert f'href="/?{open_qs}&plate=no"' in html           # open without a plate
    assert f'href="/?{open_qs}&priority=low"' in html
    assert f'href="/?{open_qs}&cat=brakes"' in html
    assert 'id="open-only"' in html
    assert 'href="/reports/tickets?kind=new&days=30"' in html
    assert 'href="/reports/tickets?kind=resolved&days=30"' in html


async def test_period_tickets_match_headline_counts(seeded, db_session):
    start, end, first, last = reports.resolve_range(NAIROBI, "30")
    new = await reports.period_tickets(db_session, None, start, end, "new")
    resolved = await reports.period_tickets(db_session, None, start, end, "resolved")
    ov = await reports.fleet_overview(db_session, None, start, end, first, last, NAIROBI)
    assert len(new) == ov["new_in_range"] == 5
    assert [t["incident"].id for t in resolved] == [seeded["b"]]
    assert len(resolved) == ov["resolved_in_range"]


async def test_period_tickets_page(seeded, fleet_admin):
    r = await fleet_admin.get("/reports/tickets?kind=resolved&days=30")
    assert r.status_code == 200
    html = unescape(r.text)
    assert f'href="/archive?ticket={seeded["b"]}"' in html
    assert 'href="/reports/plate/KBBB222B"' in html
    assert 'href="/reports?days=30"' in html            # back link keeps the range

    html = unescape((await fleet_admin.get("/reports/tickets?kind=new&from=2020-01-01&to=2099-01-01")).text)
    assert html.count('class="ticket-id"') == 6          # custom range covers every ticket
    assert 'href="/reports?from=2020-01-01&to=2099-01-01"' in html


async def test_period_tickets_page_404s_and_scoping(seeded, fleet_admin, fleet_g1_user, non_fleet_admin):
    assert (await fleet_admin.get("/reports/tickets?kind=bogus")).status_code == 404
    assert (await non_fleet_admin.get("/reports/tickets")).status_code == 404
    html = (await fleet_g1_user.get("/reports/tickets?kind=resolved")).text
    assert "KBBB222B" not in html                        # g2 only
