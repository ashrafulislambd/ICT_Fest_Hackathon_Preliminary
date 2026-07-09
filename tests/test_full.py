"""Comprehensive black-box test of the CoWork API against the business rules
in the problem statement. Run with: pytest -q test_full.py
"""
import concurrent.futures
import math
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def uniq(prefix="x"):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def iso(dt: datetime) -> str:
    return dt.isoformat()


def future(hours=1, minutes=0):
    return datetime.now(timezone.utc) + timedelta(hours=hours, minutes=minutes)


def register(org, username, password="pw12345"):
    r = client.post("/auth/register", json={"org_name": org, "username": username, "password": password})
    return r


def login(org, username, password="pw12345"):
    r = client.post("/auth/login", json={"org_name": org, "username": username, "password": password})
    return r


def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def make_org_admin(org=None):
    org = org or uniq("org")
    reg = register(org, "admin1")
    assert reg.status_code == 201, reg.text
    assert reg.json()["role"] == "admin"
    tok = login(org, "admin1").json()["access_token"]
    return org, tok


def make_room(admin_token, rate=1000, capacity=4):
    r = client.post("/rooms", json={"name": uniq("room"), "capacity": capacity, "hourly_rate_cents": rate}, headers=auth_headers(admin_token))
    assert r.status_code == 201, r.text
    return r.json()


def create_booking(token, room_id, start_hours, duration_hours, expect=201):
    start = future(hours=start_hours)
    end = start + timedelta(hours=duration_hours)
    r = client.post("/bookings", json={"room_id": room_id, "start_time": iso(start), "end_time": iso(end)}, headers=auth_headers(token))
    if expect is not None:
        assert r.status_code == expect, r.text
    return r


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_register_creates_admin_then_member_join():
    org = uniq("org")
    r1 = register(org, "alice")
    assert r1.status_code == 201
    assert r1.json()["role"] == "admin"

    r2 = register(org, "bob")
    assert r2.status_code == 201
    assert r2.json()["role"] == "member"


def test_register_duplicate_username_conflict():
    org = uniq("org")
    register(org, "alice")
    r = register(org, "alice")
    assert r.status_code == 409
    assert r.json()["code"] == "USERNAME_TAKEN"


def test_login_bad_credentials():
    org, tok = make_org_admin()
    r = login(org, "admin1", password="wrong")
    assert r.status_code == 401
    assert r.json()["code"] == "INVALID_CREDENTIALS"


def test_access_token_expiry_claim():
    import jwt as pyjwt
    org, tok = make_org_admin()
    payload = pyjwt.decode(tok, options={"verify_signature": False})
    assert payload["exp"] - payload["iat"] == 900, payload


def test_logout_invalidates_access_token():
    org, tok = make_org_admin()
    r = client.get("/rooms", headers=auth_headers(tok))
    assert r.status_code == 200
    r = client.post("/auth/logout", headers=auth_headers(tok))
    assert r.status_code == 200
    r = client.get("/rooms", headers=auth_headers(tok))
    assert r.status_code == 401


def test_refresh_rotates_and_old_refresh_token_rejected():
    org, tok = make_org_admin()
    login_resp = login(org, "admin1").json()
    refresh_token = login_resp["refresh_token"]

    r1 = client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert r1.status_code == 200
    new_refresh = r1.json()["refresh_token"]
    assert new_refresh != refresh_token

    # reuse of old refresh token must fail
    r2 = client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert r2.status_code == 401

    # new refresh token still works
    r3 = client.post("/auth/refresh", json={"refresh_token": new_refresh})
    assert r3.status_code == 200


# ---------------------------------------------------------------------------
# Multi-tenancy
# ---------------------------------------------------------------------------

def test_cross_org_room_is_404():
    org1, tok1 = make_org_admin()
    org2, tok2 = make_org_admin()
    room = make_room(tok1)
    r = client.get(f"/rooms/{room['id']}/stats", headers=auth_headers(tok2))
    assert r.status_code == 404
    assert r.json()["code"] == "ROOM_NOT_FOUND"


def test_cross_org_booking_is_404():
    org1, tok1 = make_org_admin()
    org2, tok2 = make_org_admin()
    room1 = make_room(tok1)
    b = create_booking(tok1, room1["id"], 5, 1).json()
    r = client.get(f"/bookings/{b['id']}", headers=auth_headers(tok2))
    assert r.status_code == 404


def test_member_cannot_see_other_members_booking():
    org, admin_tok = make_org_admin()
    register(org, "member1")
    register(org, "member2")
    tok1 = login(org, "member1").json()["access_token"]
    tok2 = login(org, "member2").json()["access_token"]
    room = make_room(admin_tok)
    b = create_booking(tok1, room["id"], 5, 1).json()
    r = client.get(f"/bookings/{b['id']}", headers=auth_headers(tok2))
    assert r.status_code == 404
    r = client.post(f"/bookings/{b['id']}/cancel", headers=auth_headers(tok2))
    assert r.status_code == 404


def test_admin_can_cancel_any_org_booking():
    org, admin_tok = make_org_admin()
    register(org, "member1")
    tok1 = login(org, "member1").json()["access_token"]
    room = make_room(admin_tok)
    b = create_booking(tok1, room["id"], 5, 1).json()
    r = client.post(f"/bookings/{b['id']}/cancel", headers=auth_headers(admin_tok))
    assert r.status_code == 200


def test_non_admin_cannot_create_room():
    org, admin_tok = make_org_admin()
    register(org, "member1")
    tok1 = login(org, "member1").json()["access_token"]
    r = client.post("/rooms", json={"name": "x", "capacity": 2, "hourly_rate_cents": 100}, headers=auth_headers(tok1))
    assert r.status_code == 403
    assert r.json()["code"] == "FORBIDDEN"


# ---------------------------------------------------------------------------
# Booking validation
# ---------------------------------------------------------------------------

def test_booking_price_calculation():
    org, tok = make_org_admin()
    room = make_room(tok, rate=1500)
    r = create_booking(tok, room["id"], 5, 3)
    assert r.json()["price_cents"] == 4500


def test_booking_past_start_rejected_no_grace():
    org, tok = make_org_admin()
    room = make_room(tok)
    start = datetime.now(timezone.utc) - timedelta(seconds=1)
    end = start + timedelta(hours=1)
    r = client.post("/bookings", json={"room_id": room["id"], "start_time": iso(start), "end_time": iso(end)}, headers=auth_headers(tok))
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_BOOKING_WINDOW"


def test_booking_start_must_be_strictly_future_no_grace_window():
    # start "now" (i.e. barely in the past by request-processing time) must fail;
    # this specifically targets the removed 300s grace window.
    org, tok = make_org_admin()
    room = make_room(tok)
    start = datetime.now(timezone.utc) - timedelta(seconds=120)
    end = start + timedelta(hours=1)
    r = client.post("/bookings", json={"room_id": room["id"], "start_time": iso(start), "end_time": iso(end)}, headers=auth_headers(tok))
    assert r.status_code == 400


def test_booking_non_whole_hour_duration_rejected():
    org, tok = make_org_admin()
    room = make_room(tok)
    start = future(hours=5)
    end = start + timedelta(minutes=30)
    r = client.post("/bookings", json={"room_id": room["id"], "start_time": iso(start), "end_time": iso(end)}, headers=auth_headers(tok))
    assert r.status_code == 400


def test_booking_zero_duration_rejected():
    org, tok = make_org_admin()
    room = make_room(tok)
    start = future(hours=5)
    r = client.post("/bookings", json={"room_id": room["id"], "start_time": iso(start), "end_time": iso(start)}, headers=auth_headers(tok))
    assert r.status_code == 400


def test_booking_negative_duration_rejected():
    org, tok = make_org_admin()
    room = make_room(tok)
    start = future(hours=5)
    end = start - timedelta(hours=1)
    r = client.post("/bookings", json={"room_id": room["id"], "start_time": iso(start), "end_time": iso(end)}, headers=auth_headers(tok))
    assert r.status_code == 400


def test_booking_over_max_duration_rejected():
    org, tok = make_org_admin()
    room = make_room(tok)
    r = create_booking(tok, room["id"], 5, 9, expect=400)
    assert r.json()["code"] == "INVALID_BOOKING_WINDOW"


def test_booking_max_duration_boundary_ok():
    org, tok = make_org_admin()
    room = make_room(tok)
    r = create_booking(tok, room["id"], 5, 8, expect=201)


def test_booking_min_duration_boundary_ok():
    org, tok = make_org_admin()
    room = make_room(tok)
    r = create_booking(tok, room["id"], 5, 1, expect=201)


def test_back_to_back_bookings_allowed():
    org, tok = make_org_admin()
    room = make_room(tok)
    start = future(hours=5)
    mid = start + timedelta(hours=1)
    end = mid + timedelta(hours=1)
    r1 = client.post("/bookings", json={"room_id": room["id"], "start_time": iso(start), "end_time": iso(mid)}, headers=auth_headers(tok))
    assert r1.status_code == 201, r1.text
    r2 = client.post("/bookings", json={"room_id": room["id"], "start_time": iso(mid), "end_time": iso(end)}, headers=auth_headers(tok))
    assert r2.status_code == 201, r2.text


def test_overlapping_booking_conflict():
    org, tok = make_org_admin()
    room = make_room(tok)
    start = future(hours=10)
    end = start + timedelta(hours=2)
    r1 = client.post("/bookings", json={"room_id": room["id"], "start_time": iso(start), "end_time": iso(end)}, headers=auth_headers(tok))
    assert r1.status_code == 201
    overlap_start = start + timedelta(minutes=30)
    overlap_end = overlap_start + timedelta(hours=1)
    r2 = client.post("/bookings", json={"room_id": room["id"], "start_time": iso(overlap_start), "end_time": iso(overlap_end)}, headers=auth_headers(tok))
    assert r2.status_code == 409
    assert r2.json()["code"] == "ROOM_CONFLICT"


def test_utc_offset_normalization():
    org, tok = make_org_admin()
    room = make_room(tok)
    # +06:00 offset input; should be normalized to UTC before storage/comparison
    start_utc = future(hours=20)
    start_local = start_utc.astimezone(timezone(timedelta(hours=6)))
    end_local = (start_utc + timedelta(hours=1)).astimezone(timezone(timedelta(hours=6)))
    r = client.post("/bookings", json={"room_id": room["id"], "start_time": start_local.isoformat(), "end_time": end_local.isoformat()}, headers=auth_headers(tok))
    assert r.status_code == 201, r.text
    got_start = datetime.fromisoformat(r.json()["start_time"])
    assert abs((got_start - start_utc).total_seconds()) < 2, (got_start, start_utc)
    assert r.json()["start_time"].endswith("+00:00") or r.json()["start_time"].endswith("Z")


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------

def test_quota_exceeded_within_24h_window():
    org, tok = make_org_admin()
    room = make_room(tok)
    for h in (1, 3, 5):
        create_booking(tok, room["id"], h, 1, expect=201)
    r = create_booking(tok, room["id"], 7, 1, expect=409)
    assert r.json()["code"] == "QUOTA_EXCEEDED"


def test_quota_not_counted_outside_24h_window():
    org, tok = make_org_admin()
    room = make_room(tok)
    for h in (1, 3, 5):
        create_booking(tok, room["id"], h, 1, expect=201)
    # 4th booking starting after the 24h window should still succeed
    create_booking(tok, room["id"], 30, 1, expect=201)


# ---------------------------------------------------------------------------
# Pagination & ordering
# ---------------------------------------------------------------------------

def test_pagination_and_ordering():
    org, tok = make_org_admin()
    room = make_room(tok)
    hours = [2, 10, 20, 30, 40]
    created_ids = []
    for h in hours:
        b = create_booking(tok, room["id"], h, 1, expect=201).json()
        created_ids.append(b["id"])

    page1 = client.get("/bookings?page=1&limit=2", headers=auth_headers(tok)).json()
    page2 = client.get("/bookings?page=2&limit=2", headers=auth_headers(tok)).json()
    page3 = client.get("/bookings?page=3&limit=2", headers=auth_headers(tok)).json()

    assert page1["total"] == 5
    all_items = page1["items"] + page2["items"] + page3["items"]
    ids_seen = [i["id"] for i in all_items]
    assert len(ids_seen) == len(set(ids_seen)) == 5, ids_seen
    assert set(ids_seen) == set(created_ids)

    starts = [i["start_time"] for i in all_items]
    assert starts == sorted(starts), starts


# ---------------------------------------------------------------------------
# Cancellation / refunds
# ---------------------------------------------------------------------------

def test_cancel_full_refund_at_48h():
    org, tok = make_org_admin()
    room = make_room(tok, rate=1000)
    # a couple minutes of slack so processing delay doesn't drift us under 48h by cancel time
    b = create_booking(tok, room["id"], 48.05, 1, expect=201).json()
    r = client.post(f"/bookings/{b['id']}/cancel", headers=auth_headers(tok))
    assert r.status_code == 200
    assert r.json()["refund_percent"] == 100, r.json()


def test_cancel_50_percent_between_24_and_48h():
    org, tok = make_org_admin()
    room = make_room(tok, rate=1000)
    b = create_booking(tok, room["id"], 30, 1, expect=201).json()
    r = client.post(f"/bookings/{b['id']}/cancel", headers=auth_headers(tok))
    assert r.status_code == 200
    assert r.json()["refund_percent"] == 50


def test_cancel_zero_percent_under_24h():
    org, tok = make_org_admin()
    room = make_room(tok, rate=1000)
    b = create_booking(tok, room["id"], 5, 1, expect=201).json()
    r = client.post(f"/bookings/{b['id']}/cancel", headers=auth_headers(tok))
    assert r.status_code == 200
    assert r.json()["refund_percent"] == 0
    assert r.json()["refund_amount_cents"] == 0


def test_cancel_already_cancelled_conflict():
    org, tok = make_org_admin()
    room = make_room(tok)
    b = create_booking(tok, room["id"], 5, 1, expect=201).json()
    r1 = client.post(f"/bookings/{b['id']}/cancel", headers=auth_headers(tok))
    assert r1.status_code == 200
    r2 = client.post(f"/bookings/{b['id']}/cancel", headers=auth_headers(tok))
    assert r2.status_code == 409
    assert r2.json()["code"] == "ALREADY_CANCELLED"


def test_refund_amount_matches_refund_log():
    org, tok = make_org_admin()
    room = make_room(tok, rate=999)  # odd cents to exercise rounding
    b = create_booking(tok, room["id"], 30, 3, expect=201).json()  # 50% tier, price 2997
    r = client.post(f"/bookings/{b['id']}/cancel", headers=auth_headers(tok))
    assert r.status_code == 200
    resp_amount = r.json()["refund_amount_cents"]
    detail = client.get(f"/bookings/{b['id']}", headers=auth_headers(tok)).json()
    assert len(detail["refunds"]) == 1
    assert detail["refunds"][0]["amount_cents"] == resp_amount


def test_refund_rounding_half_up():
    org, tok = make_org_admin()
    # price such that 50% gives an exact half-cent: price_cents=2001 -> 50% = 1000.5 -> round up to 1001
    room = make_room(tok, rate=667)  # 3 hours -> 2001 cents
    b = create_booking(tok, room["id"], 30, 3, expect=201).json()
    assert b["price_cents"] == 2001
    r = client.post(f"/bookings/{b['id']}/cancel", headers=auth_headers(tok))
    assert r.json()["refund_percent"] == 50
    assert r.json()["refund_amount_cents"] == 1001, r.json()


# ---------------------------------------------------------------------------
# Availability / stats / usage-report + cache freshness
# ---------------------------------------------------------------------------

def test_availability_reflects_state_immediately():
    org, tok = make_org_admin()
    room = make_room(tok)
    date = (datetime.now(timezone.utc) + timedelta(days=2)).date().isoformat()
    start = datetime.fromisoformat(date + "T10:00:00+00:00")
    end = start + timedelta(hours=1)
    # touch endpoint first to warm any cache
    client.get(f"/rooms/{room['id']}/availability?date={date}", headers=auth_headers(tok))
    r = client.post("/bookings", json={"room_id": room["id"], "start_time": iso(start), "end_time": iso(end)}, headers=auth_headers(tok))
    assert r.status_code == 201, r.text
    avail = client.get(f"/rooms/{room['id']}/availability?date={date}", headers=auth_headers(tok)).json()
    assert len(avail["busy"]) == 1, avail

    booking_id = r.json()["id"]
    client.post(f"/bookings/{booking_id}/cancel", headers=auth_headers(tok))
    avail2 = client.get(f"/rooms/{room['id']}/availability?date={date}", headers=auth_headers(tok)).json()
    assert len(avail2["busy"]) == 0, avail2


def test_usage_report_reflects_state_immediately():
    org, tok = make_org_admin()
    room = make_room(tok, rate=500)
    frm = datetime.now(timezone.utc).date().isoformat()
    to = (datetime.now(timezone.utc) + timedelta(days=3)).date().isoformat()
    client.get(f"/admin/usage-report?from={frm}&to={to}", headers=auth_headers(tok))
    create_booking(tok, room["id"], 5, 2, expect=201)
    report = client.get(f"/admin/usage-report?from={frm}&to={to}", headers=auth_headers(tok)).json()
    row = next(r for r in report["rooms"] if r["room_id"] == room["id"])
    assert row["confirmed_bookings"] == 1
    assert row["revenue_cents"] == 1000


def test_usage_report_includes_zero_booking_rooms():
    org, tok = make_org_admin()
    room = make_room(tok)
    frm = datetime.now(timezone.utc).date().isoformat()
    to = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    report = client.get(f"/admin/usage-report?from={frm}&to={to}", headers=auth_headers(tok)).json()
    assert any(r["room_id"] == room["id"] and r["confirmed_bookings"] == 0 for r in report["rooms"])


def test_room_stats_live():
    org, tok = make_org_admin()
    room = make_room(tok, rate=200)
    b = create_booking(tok, room["id"], 5, 2, expect=201).json()
    stats = client.get(f"/rooms/{room['id']}/stats", headers=auth_headers(tok)).json()
    assert stats["total_confirmed_bookings"] == 1
    assert stats["total_revenue_cents"] == 400
    client.post(f"/bookings/{b['id']}/cancel", headers=auth_headers(tok))
    stats2 = client.get(f"/rooms/{room['id']}/stats", headers=auth_headers(tok)).json()
    assert stats2["total_confirmed_bookings"] == 0
    assert stats2["total_revenue_cents"] == 0


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def test_export_scoped_to_org():
    org1, tok1 = make_org_admin()
    org2, tok2 = make_org_admin()
    room1 = make_room(tok1)
    room2 = make_room(tok2)
    create_booking(tok1, room1["id"], 5, 1, expect=201)
    create_booking(tok2, room2["id"], 5, 1, expect=201)

    r = client.get("/admin/export?include_all=true", headers=auth_headers(tok1))
    assert r.status_code == 200
    rows = [l.split(",") for l in r.text.splitlines()[1:] if l.strip()]
    room_ids_in_export = {row[2] for row in rows}
    assert str(room2["id"]) not in room_ids_in_export


def test_export_include_all_room_id_cross_org_blocked():
    org1, tok1 = make_org_admin()
    org2, tok2 = make_org_admin()
    room2 = make_room(tok2)
    create_booking(tok2, room2["id"], 5, 1, expect=201)

    r = client.get(f"/admin/export?include_all=true&room_id={room2['id']}", headers=auth_headers(tok1))
    assert r.status_code == 200
    lines = [l for l in r.text.splitlines() if l.strip()]
    # header only, no rows since room2 belongs to a different org
    assert len(lines) == 1, r.text


def test_export_header_exact():
    org, tok = make_org_admin()
    r = client.get("/admin/export", headers=auth_headers(tok))
    header = r.text.splitlines()[0]
    assert header == "id,reference_code,room_id,user_id,start_time,end_time,status,price_cents"


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def test_rate_limit_21st_request_blocked():
    org, tok = make_org_admin()
    room = make_room(tok)
    # 20 requests should be fine even if they individually conflict/fail validation;
    # rate limit counts *all* POST /bookings requests regardless of outcome.
    last_status = None
    for i in range(21):
        start = future(hours=100 + i)
        end = start + timedelta(hours=1)
        r = client.post("/bookings", json={"room_id": room["id"], "start_time": iso(start), "end_time": iso(end)}, headers=auth_headers(tok))
        last_status = r.status_code
    assert last_status == 429, last_status


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

def test_concurrent_conflicting_bookings_only_one_succeeds():
    org, tok = make_org_admin()
    room = make_room(tok)
    start = future(hours=60)
    end = start + timedelta(hours=1)

    def attempt():
        return client.post("/bookings", json={"room_id": room["id"], "start_time": iso(start), "end_time": iso(end)}, headers=auth_headers(tok))

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda _: attempt(), range(8)))

    statuses = [r.status_code for r in results]
    assert statuses.count(201) == 1, statuses
    assert statuses.count(409) == 7, statuses


def test_concurrent_quota_never_exceeded():
    org, tok = make_org_admin()
    room = make_room(tok)

    def attempt(i):
        start = future(hours=1 + i * 2)  # all within 24h window, non-overlapping
        end = start + timedelta(hours=1)
        return client.post("/bookings", json={"room_id": room["id"], "start_time": iso(start), "end_time": iso(end)}, headers=auth_headers(tok))

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(attempt, range(8)))

    successes = sum(1 for r in results if r.status_code == 201)
    assert successes == 3, [r.status_code for r in results]


def test_concurrent_reference_codes_unique():
    org, tok = make_org_admin()
    room = make_room(tok)

    def attempt(i):
        start = future(hours=200 + i * 2)
        end = start + timedelta(hours=1)
        return client.post("/bookings", json={"room_id": room["id"], "start_time": iso(start), "end_time": iso(end)}, headers=auth_headers(tok))

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(attempt, range(10)))

    codes = [r.json()["reference_code"] for r in results if r.status_code == 201]
    assert len(codes) == len(set(codes)), codes


def test_concurrent_cancel_same_booking_single_refund():
    org, tok = make_org_admin()
    room = make_room(tok)
    b = create_booking(tok, room["id"], 5, 1, expect=201).json()

    def attempt():
        return client.post(f"/bookings/{b['id']}/cancel", headers=auth_headers(tok))

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda _: attempt(), range(6)))

    statuses = [r.status_code for r in results]
    assert statuses.count(200) == 1, statuses
    assert statuses.count(409) == 5, statuses

    detail = client.get(f"/bookings/{b['id']}", headers=auth_headers(tok)).json()
    assert len(detail["refunds"]) == 1, detail["refunds"]


def test_concurrent_stats_consistent_after_burst():
    org, tok = make_org_admin()
    room = make_room(tok, rate=100)

    def create(i):
        start = future(hours=300 + i * 2)
        end = start + timedelta(hours=1)
        return client.post("/bookings", json={"room_id": room["id"], "start_time": iso(start), "end_time": iso(end)}, headers=auth_headers(tok))

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(create, range(10)))

    created = [r.json() for r in results if r.status_code == 201]
    stats = client.get(f"/rooms/{room['id']}/stats", headers=auth_headers(tok)).json()
    assert stats["total_confirmed_bookings"] == len(created)
    assert stats["total_revenue_cents"] == sum(c["price_cents"] for c in created)


def test_liveness_concurrent_create_and_cancel_no_hang():
    org, tok = make_org_admin()
    room = make_room(tok)
    bookings = []
    for i in range(6):
        b = create_booking(tok, room["id"], 400 + i * 2, 1, expect=201).json()
        bookings.append(b)

    def cancel(b):
        return client.post(f"/bookings/{b['id']}/cancel", headers=auth_headers(tok))

    def create_new(i):
        start = future(hours=500 + i * 2)
        end = start + timedelta(hours=1)
        return client.post("/bookings", json={"room_id": room["id"], "start_time": iso(start), "end_time": iso(end)}, headers=auth_headers(tok))

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        futs = [ex.submit(cancel, b) for b in bookings] + [ex.submit(create_new, i) for i in range(6)]
        done, not_done = concurrent.futures.wait(futs, timeout=30)
    assert not not_done, "some requests hung / deadlocked"
