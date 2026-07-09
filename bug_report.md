# CoWork API — Bug Report (detailed)

Detailed, uniformly-structured write-up of every bug found and fixed. Line numbers
refer to the **original (broken) code on the `main` branch**. All 26 fixes are
verified by `tests/test_full.py` (52 tests)

**Per-bug structure** 

- **Location** — file(s) and original line(s)
- **Spec rule** — the business rule / contract clause violated
- **Root cause** — the original code and why it was wrong
- **Incorrect behavior** — what the API did that was observably wrong
- **Reproduce** — concrete steps (+ benchmark criterion ID)
- **Fix** — what was changed

---

## Bug 14 — Missing concurrency guards on booking create/cancel

- **Location:** `app/routers/bookings.py` — `create_booking` (conflict/quota check + insert) and `cancel_booking` (status check + refund)
- **Spec rule:** Rules 3, 4, 6 — conflict, quota and single-refund must hold "under concurrent requests."
- **Root cause:** The check-then-write sequences ran with no mutual exclusion; two requests could both pass their checks before either committed. (The deliberate `time.sleep()` helpers widen this window.)
- **Incorrect behavior:** Concurrent requests could double-book a slot, push a member past the 3-booking quota, or produce two refunds for one booking.
- **Reproduce:** Fire 8 parallel identical-slot creates → more than one **201** (should be exactly 1). (Criteria **CONC-01/02/04**)
- **Fix:** A module-level `_booking_lock` wraps the conflict/quota-check + insert and the cancel status-check + refund; `cancel_booking` re-reads with `db.refresh(booking)` inside the lock.

## Bug 15 — Rate limiter has an unlocked read-modify-write

- **Location:** `app/services/ratelimit.py:20–25` (`record_and_check`)
- **Spec rule:** Rule 5 — 20 requests / rolling 60 s / user, "must hold under concurrent requests."
- **Root cause:** The per-user bucket was read, trimmed, appended and written back with no lock (and a `_settle_pause()` sleep in the middle widening the race).
- **Incorrect behavior:** Concurrent requests overwrote each other's bucket, so more than 20 requests/60s slipped through.
- **Reproduce:** Fire >20 parallel `POST /bookings` for one user → more than 20 are admitted. (Criterion **CONC-06**)
- **Fix:** Wrapped the whole read-trim-append-write in a `threading.Lock`; compute `exceeded` inside the lock, raise 429 outside.

## Bug 16 — Reference-code counter race → duplicate codes

- **Location:** `app/services/reference.py:18` (`next_reference_code`)
- **Spec rule:** Rule 7 — "Every booking's reference code is unique, including under concurrent creation."
- **Root cause:** Read counter → sleep (`_format_pause`) → write `+1`, all unlocked, so two callers could read the same value.
- **Incorrect behavior:** Concurrent booking creation produced duplicate reference codes.
- **Reproduce:** Fire 10 parallel creates → duplicate `reference_code` values appear. (Criterion **CONC-03**)
- **Fix:** `threading.Lock` around the read-increment; format the string after releasing.

## Bug 17 — Stats counters race → drift from bookings

- **Location:** `app/services/stats.py` (`record_create`, `record_cancel`, `get`)
- **Spec rule:** Rule 14 — stats "always consistent with the bookings themselves, including after bursts of concurrent activity."
- **Root cause:** Read-modify-write on the shared `_stats` dict with no lock (plus an `_aggregate_pause` sleep).
- **Incorrect behavior:** Concurrent creates/cancels lost updates, leaving room stats inconsistent with the actual bookings.
- **Reproduce:** Fire 10 parallel creates, then read `GET /rooms/{id}/stats` → count/revenue below the true totals. (Criterion **CONC-07**)
- **Fix:** `threading.Lock` around all three operations.

## Bug 19 — Notification lock-ordering deadlock

- **Location:** `app/services/notifications.py:31–35` (`notify_cancelled`)
- **Spec rule:** Rule 16 — "no combination of concurrent valid requests may hang the service."
- **Root cause:** Inconsistent lock acquisition order:
  ```python
  # notify_created:   _email_lock → _audit_lock
  # notify_cancelled: _audit_lock → _email_lock   (reversed)
  ```
- **Incorrect behavior:** A concurrent create + cancel could each hold one lock and wait for the other — a classic deadlock that hangs both requests.
- **Reproduce:** Run many concurrent create+cancel pairs → some requests never return. (Criterion **CONC-10**)
- **Fix:** Reordered `notify_cancelled` to acquire `_email_lock` then `_audit_lock`, matching `notify_created`.

## Bug 22 — Registration race (TOCTOU on org/username creation)

- **Location:** `app/routers/auth.py:23` (`register`)
- **Spec rule:** Rules 15 & 16 — correct role assignment / 409 on duplicates, and no unhandled crash.
- **Root cause:** Check-then-insert for both the org and the username with no lock. Two concurrent requests for the same new org name (or same org+username) both passed the existence check before either committed; the DB unique constraints then rejected the loser with an unhandled `IntegrityError`.
- **Incorrect behavior:** Concurrent identical registrations produced raw **500**s (unhandled `IntegrityError`) instead of a clean role assignment / `409 USERNAME_TAKEN`.
- **Reproduce:** Fire 8 parallel identical registrations → some return **500** (should be exactly one 201, the rest 409, no 500s). (Criterion **CONC-09**)
- **Fix:** A `_register_lock` serializes the whole lookup-or-create + check-then-insert sequence, matching the locking pattern used elsewhere.

## Bug 24 — Refresh single-use check is a TOCTOU race

- **Location:** `app/auth.py` — the check + revoke steps used by `/auth/refresh`
- **Spec rule:** Rule 8 — refresh tokens single-use "under concurrent use."
- **Root cause:** The single-use mechanism (Bug 3's fix) did the "is it revoked?" check and the "mark revoked" write as two separate, unguarded steps. Concurrent requests with the same token all passed the check before any recorded it.
- **Incorrect behavior:** Firing the same refresh token concurrently yielded **multiple** 200s (each minting a new token pair) instead of exactly one.
- **Reproduce:** Fire 6 parallel `/auth/refresh` with one refresh token → 2+ succeed (verified live: 2×200 before fix). (Criterion **CONC-08**)
- **Fix:** Consolidated check+mark into one atomic `redeem_refresh_token(payload)` guarded by `_refresh_redemption_lock`.

## Bug 2 — Logout never invalidates the token (checks `sub`, not `jti`)

- **Location:** `app/auth.py:97` (`get_token_payload`)
- **Spec rule:** Rule 8 — "Logout immediately invalidates the presented access token (subsequent use → 401)."
- **Root cause:** `revoke_access_token` stores the token's `jti`, but the guard checked the `sub` claim:
  ```python
  if payload.get("sub") in _revoked_tokens:   # sub is the user id; jti was stored
  ```
  `sub` (a user id) is never equal to a stored `jti`, so the membership test never matched.
- **Incorrect behavior:** After `POST /auth/logout`, the same access token kept working.
- **Reproduce:** Login → `GET /rooms` (200) → `POST /auth/logout` → reuse token on `GET /rooms` → still **200** (should be 401). (Criterion **AUTH-08**)
- **Fix:** Compare on the correct claim: `if payload.get("jti") in _revoked_tokens:`.

## Bug 3 — Refresh tokens are not single-use

- **Location:** `app/routers/auth.py:82` (`refresh`)
- **Spec rule:** Rule 8 — "Refresh tokens are single-use … reuse → 401."
- **Root cause:** The refresh endpoint issued a new access+refresh pair but never recorded/invalidated the presented refresh token, so it could be redeemed again.
- **Incorrect behavior:** The same refresh token could be replayed indefinitely, each time minting fresh tokens.
- **Reproduce:** Refresh with a token (200), then refresh again with the **same** token → still **200** (should be 401). (Criterion **AUTH-11**)
- **Fix:** Introduced single-use redemption (`redeem_refresh_token`) invoked in `refresh` before issuing new tokens; a consumed `jti` is recorded so a replay → 401. (See also **Bug 24** for the concurrency-safe form.)

## Bug 4 — Offset-aware datetimes not converted to UTC

- **Location:** `app/timeutils.py:13` (`parse_input_datetime`)
- **Spec rule:** Rule 1 — "Input datetimes carrying a UTC offset must be converted to UTC before storage or comparison."
- **Root cause:** For tz-aware input the offset was **stripped**, not converted:
  ```python
  if dt.tzinfo is not None:
      dt = dt.replace(tzinfo=None)   # 10:00+06:00 becomes naive 10:00, not 04:00Z
  ```
- **Incorrect behavior:** A `10:00+06:00` input was stored/compared as `10:00Z` instead of `04:00Z`, corrupting price windows, conflict detection, quota windows, availability and reports for any non-UTC client.
- **Reproduce:** Create a booking with `start_time` in `+06:00`; the returned `start_time` is 6 hours off the intended UTC instant. (Criterion **TIME-01**)
- **Fix:** `dt = dt.astimezone(timezone.utc).replace(tzinfo=None)` — convert first, then normalize to naive UTC.

## Bug 7 — Missing minimum-duration and `end > start` validation

- **Location:** `app/routers/bookings.py:93` (`create_booking`)
- **Spec rule:** Rule 2 — duration minimum 1 hour; `end_time` strictly after `start_time`.
- **Root cause:** Only the maximum was checked (`if duration_hours > MAX_DURATION_HOURS`). `MIN_DURATION_HOURS` was defined but unused, and there was no explicit `end > start` guard, so a zero or negative duration passed the "whole number of hours" check.
- **Incorrect behavior:** 0-hour bookings (`end == start`) and negative-duration bookings (`end < start`) were created with zero/negative `price_cents`.
- **Reproduce:** Create a booking with `end_time == start_time` → **201** (should be 400). (Criteria **WIN-02**, **WIN-06**)
- **Fix:** Added `if end <= start: 400` and `if duration_hours < MIN_DURATION_HOURS or duration_hours > MAX_DURATION_HOURS: 400`.

## Bug 8 — Back-to-back bookings rejected as conflicts

- **Location:** `app/routers/bookings.py:50` (`_has_conflict`)
- **Spec rule:** Rule 3 — overlap iff `existing.start < new.end AND new.start < existing.end`; back-to-back allowed.
- **Root cause:** Non-strict comparison treated touching intervals as overlapping:
  ```python
  if b.start_time <= end and start <= b.end_time:   # <= should be <
  ```
- **Incorrect behavior:** A booking starting exactly when another ends (`existing.end == new.start`) returned 409 `ROOM_CONFLICT`.
- **Reproduce:** Book `[10:00, 11:00]`, then `[11:00, 12:00]` in the same room → second is **409** (should be 201). (Criterion **CONF-03**)
- **Fix:** Strict comparison: `if b.start_time < end and start < b.end_time:`.

## Bug 9 — `GET /bookings` wrong order, wrong page offset, ignores `limit`

- **Location:** `app/routers/bookings.py:137–139` (`list_bookings`)
- **Spec rule:** Rule 11 — ascending by start_time (ties by id asc); pages never skip/repeat; `limit` honored.
- **Root cause:** Three defects in one query:
  ```python
  base.order_by(Booking.start_time.desc(), Booking.id.asc())  # wrong: descending
      .offset(page * limit)                                    # wrong: skips a page (page 1 → offset=limit)
      .limit(10)                                               # wrong: ignores caller's limit
  ```
- **Incorrect behavior:** Items returned newest-first; page 1 skipped the true first page; `limit` had no effect (always 10).
- **Reproduce:** Create 5 bookings, request `?page=1&limit=2` → returns items 3–4 in descending order instead of items 1–2 ascending. (Criteria **PAGE-01/02/04/06**)
- **Fix:** `order_by(start_time.asc(), id.asc())`, `offset((page - 1) * limit)`, `limit(limit)`.

## Bug 10b — `GET /bookings/{id}` missing owner/admin visibility check

- **Location:** `app/routers/bookings.py:151–163` (`get_booking`)
- **Spec rule:** Rule 10 — "Members may read … only their own bookings (another member's booking id → 404 BOOKING_NOT_FOUND)."
- **Root cause:** The query filtered by org only; unlike `cancel_booking`, it lacked the owner/admin guard.
- **Incorrect behavior:** Any member could read any other member's booking in the same org by id.
- **Reproduce:** member1 creates a booking; member2 does `GET /bookings/{that_id}` → **200** with member1's data (should be 404). (Criterion **VIS-01**)
- **Fix:** Added `if user.role != "admin" and booking.user_id != user.id: raise AppError(404, "BOOKING_NOT_FOUND", …)`.

## Bug 11 — Wrong refund tiers (<24h gives 50%, 48h boundary excluded)

- **Location:** `app/routers/bookings.py:201–206` (`cancel_booking`)
- **Spec rule:** Rule 6 — ≥48h → 100%; 24–48h → 50%; <24h → 0%.
- **Root cause:** The tier ladder was wrong in two ways:
  ```python
  if notice_hours > 48:          # excludes exactly 48h from the 100% tier
      refund_percent = 100
  elif notice >= timedelta(hours=24):
      refund_percent = 50
  else:
      refund_percent = 50        # <24h should be 0, not 50
  ```
- **Incorrect behavior:** Cancelling with <24h notice refunded 50% (should be 0%), and exactly-48h notice fell through to 50% (should be 100%).
- **Reproduce:** Cancel a booking starting 5h out → `refund_percent == 50` (should be 0). (Criterion **CANC-03**)
- **Fix:** Centralized `calculate_refund_percent(notice)` in `refunds.py`: `>=48→100`, `>=24→50`, else `0`.

## Bug 12 — Refund amount computed twice, with wrong rounding

- **Location:** `app/routers/bookings.py:208` and `app/services/refunds.py:17`
- **Spec rule:** Rule 6 — "rounds to the nearest cent, half-cents rounding up"; response amount must equal the RefundLog amount.
- **Root cause:** Two **independent** calculations that could disagree, neither doing round-half-up:
  ```python
  # router
  refund_amount_cents = round(booking.price_cents * (refund_percent / 100.0))  # banker's rounding
  # refunds.py
  amount_cents = int(refund_dollars * 100)                                     # truncation
  ```
- **Incorrect behavior:** `round()` uses banker's rounding (half-to-even) and `int()` truncates, so half-cent refunds were wrong and the returned amount could differ from the stored RefundLog amount.
- **Reproduce:** Rate 667 × 3h = 2001 cents @ 50% → correct is **1001**; `round(1000.5)` yields 1000. (Criteria **CANC-06**, **CANC-10**)
- **Fix:** Single source of truth `calculate_refund_amount_cents = math.floor(exact + 0.5)`, used by both the router (for the response) and `log_refund` (for the ledger), so they are always equal and half-up.

## Bug 13 — Stale caches after mutations

- **Location:** `app/routers/bookings.py` — `create_booking` (missing report invalidation) and `cancel_booking` (missing availability invalidation)
- **Spec rule:** Rules 12 & 13 — usage report and availability must "reflect the current state immediately."
- **Root cause:** `create_booking` invalidated only the availability cache (not the report cache); `cancel_booking` invalidated only the report cache (not the availability cache).
- **Incorrect behavior:** A new booking didn't show up in a previously-cached usage report; a cancelled booking still appeared as busy in a previously-cached availability response.
- **Reproduce:** Warm `GET /admin/usage-report`, create a booking, re-fetch → counts unchanged (**RPT-05**). Warm availability, cancel a booking, re-fetch → slot still busy (**AVL-05**).
- **Fix:** Added `cache.invalidate_report(user.org_id)` to `create_booking` and `cache.invalidate_availability(room_id, date)` to `cancel_booking`.

## Bug 20 — Export leaks other organizations' bookings

- **Location:** `app/services/export.py:22–26` (`fetch_bookings_raw`)
- **Spec rule:** Rule 9 — a user "may only ever read or act on data belonging to their own organization, on every code path."
- **Root cause:** The raw-fetch (used for `include_all=true` + `room_id`) filtered by room only, with no org scoping:
  ```python
  db.query(Booking).filter(Booking.room_id == room_id)   # no org_id check
  ```
- **Incorrect behavior:** An admin could export another org's bookings by passing a foreign `room_id`.
- **Reproduce:** As admin of org A, call `GET /admin/export?include_all=true&room_id=<org B room>` → org B's rows returned. (Criteria **TEN-07**, **EXP-04**)
- **Fix:** `fetch_bookings_raw` now joins `Room` and filters `Room.org_id == org_id`; the caller passes `org_id`.

## Bug 21 — Non-atomic cancel (refund and status change committed separately)

- **Location:** `app/services/refunds.py` (`log_refund`) + `app/routers/bookings.py` (`cancel_booking`)
- **Spec rule:** Rule 6 — "A cancelled booking has exactly one RefundLog entry" (as a durable invariant).
- **Root cause:** `log_refund` performed its own `db.commit()`, then `cancel_booking` committed the status change in a second transaction. A crash between the two commits leaves a persisted RefundLog with the booking still `confirmed`.
- **Incorrect behavior:** A mid-cancel crash could yield a refund with no cancellation; on retry the booking (still confirmed) could be refunded again — two RefundLogs for one booking.
- **Reproduce:** Structural/durability gap (requires a process crash between the two commits); the concurrency lock prevents the live double-cancel, but not the crash window.
- **Fix:** `log_refund` now only stages the row (`db.add`, no commit); the caller's single `db.commit()` writes the RefundLog and the status change atomically.

## Bug 23 — Usage-report cache goes stale when a room is created

- **Location:** `app/routers/rooms.py:43` (`create_room`)
- **Spec rule:** Rule 12 — the report lists every room (incl. zero-booking rooms) and must "reflect the current state immediately."
- **Root cause:** `create_room` committed the new room but never called `cache.invalidate_report`; only booking create/cancel invalidated it.
- **Incorrect behavior:** A room created after a report was cached stayed absent from that report until an unrelated booking event happened to invalidate the cache.
- **Reproduce:** Warm `GET /admin/usage-report` for a range, create a room, re-fetch the same range → new room missing. (Criterion **RPT-07**)
- **Fix:** Added `cache.invalidate_report(admin.org_id)` after the room commit.

## Bug 1 — Access token lifetime 60× too long

- **Location:** `app/auth.py:50` (`create_access_token`)
- **Spec rule:** Rule 8 — "Access tokens expire in exactly 900 seconds."
- **Root cause:** With `ACCESS_TOKEN_EXPIRE_MINUTES = 15`, the lifetime was computed as
  ```python
  lifetime = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60)  # = timedelta(minutes=900)
  ```
  `15 × 60 = 900` was fed into `minutes=`, yielding **900 minutes (15 hours)** = 54000 s.
- **Incorrect behavior:** Access tokens stayed valid ~15 hours instead of 900 s; `exp - iat == 54000`.
- **Reproduce:** Log in, decode the access token → `exp - iat` is 54000, not 900. (Criterion **AUTH-01**)
- **Fix:** `lifetime = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)` → 15 min = exactly 900 s.

## Bug 5 — Duplicate username silently returns the existing user

- **Location:** `app/routers/auth.py:39` (`register`)
- **Spec rule:** Rule 15 — "A duplicate username within the org → 409 USERNAME_TAKEN."
- **Root cause:** On a collision the handler **returned the existing user's record** instead of erroring:
  ```python
  if existing is not None:
      return {"user_id": existing.id, "org_id": org.id, "username": existing.username, "role": existing.role}
  ```
- **Incorrect behavior:** Re-registering a taken username returned 201 with the existing user's id/role (an information leak and a contract violation) instead of 409.
- **Reproduce:** Register `alice` in an org, then register `alice` again → **201** with alice's data (should be 409 `USERNAME_TAKEN`). (Criterion **REG-03**)
- **Fix:** `raise AppError(409, "USERNAME_TAKEN", "Username already taken in this organization")`.

## Bug 6 — Booking start-time grace window

- **Location:** `app/routers/bookings.py:86` (`create_booking`)
- **Spec rule:** Rule 2 — "start_time must be strictly in the future at request time — no grace window."
- **Root cause:** `if start <= now - timedelta(seconds=300):` allowed any start up to 5 minutes in the past.
- **Incorrect behavior:** Bookings starting up to 300 s in the past were accepted.
- **Reproduce:** Create a booking with `start_time = now − 90s` → **201** (should be 400 `INVALID_BOOKING_WINDOW`). (Criterion **WIN-07**)
- **Fix:** `if start <= now:` — strictly future, no tolerance.

## Bug 10 — `GET /bookings/{id}` overwrites `start_time` with `created_at`

- **Location:** `app/routers/bookings.py:166` (`get_booking`)
- **Spec rule:** §5 — Booking response `start_time` field must be the booking's start time.
- **Root cause:** A stray assignment clobbered the correct value:
  ```python
  response["start_time"] = iso_utc(booking.created_at)
  ```
- **Incorrect behavior:** The single-booking endpoint returned the creation timestamp in the `start_time` field.
- **Reproduce:** Create a booking, then `GET /bookings/{id}` → `start_time` equals `created_at`, not the requested start.
- **Fix:** Removed the line; `start_time` comes from `serialize_booking` unchanged.

## Bug 18 — Refund ledger truncates instead of rounding

- **Location:** `app/services/refunds.py:17` (`log_refund`)
- **Spec rule:** Rule 6 — round to nearest cent, half-cents up.
- **Root cause:** `amount_cents = int(refund_dollars * 100)` truncates toward zero (a second, independent computation from the router's — see **Bug 12**).
- **Incorrect behavior:** Stored refund amounts were under by up to a cent and could disagree with the response.
- **Reproduce:** Any refund whose exact cents value has a fractional part is truncated down. (Criterion **CANC-10**)
- **Fix:** Removed the local computation; `log_refund` now receives the single half-up amount from `calculate_refund_amount_cents`.

## Bug 25 — Malformed datetime crashes booking creation with 500

- **Location:** `app/routers/bookings.py` (`create_booking`, the `parse_input_datetime` calls)
- **Spec rule:** Rule 1 / Errors — malformed booking input should be `400 INVALID_BOOKING_WINDOW`, not a server error.
- **Root cause:** `parse_input_datetime` → `datetime.fromisoformat` raises a bare `ValueError` on non-ISO input; nothing caught it and `main.py` only handles `AppError`, so it became a 500. The two sibling date-parsing endpoints (`rooms.py::availability`, `admin.py::usage_report`) already guard this with `try/except ValueError → 400`; `create_booking` was the lone exception.
- **Incorrect behavior:** `POST /bookings` with `start_time: "not-a-date"` returned **500 Internal Server Error**.
- **Reproduce:** `POST /bookings` with a non-ISO `start_time`/`end_time` → 500 (should be 400 `INVALID_BOOKING_WINDOW`). (Criterion **TIME-05**)
- **Fix:** Wrapped both `parse_input_datetime` calls in `try/except ValueError: raise AppError(400, "INVALID_BOOKING_WINDOW", …)`, matching the sibling endpoints' convention.

---

## Summary

| # | Bug | File |
|---|-----|------|
| 14 | Missing booking concurrency guards | `routers/bookings.py` |
| 15 | Rate-limiter race | `services/ratelimit.py` |
| 16 | Reference-code race | `services/reference.py` |
| 17 | Stats race | `services/stats.py` |
| 19 | Notification deadlock | `services/notifications.py` |
| 22 | Registration race | `routers/auth.py` |
| 24 | Refresh single-use race | `auth.py` |
| 2 | Logout checks `sub` not `jti` | `auth.py` |
| 3 | Refresh not single-use | `routers/auth.py` |
| 4 | Offset not converted to UTC | `timeutils.py` |
| 7 | Missing min-duration / end>start | `routers/bookings.py` |
| 8 | Back-to-back rejected | `routers/bookings.py` |
| 9 | List order/offset/limit | `routers/bookings.py` |
| 10b | Missing booking-visibility check | `routers/bookings.py` |
| 11 | Wrong refund tiers | `routers/bookings.py` |
| 12 | Refund computed twice, bad rounding | `routers/bookings.py` + `refunds.py` |
| 13 | Stale caches after mutations | `routers/bookings.py` |
| 20 | Export cross-org leak | `services/export.py` |
| 21 | Non-atomic cancel commit | `refunds.py` + `routers/bookings.py` |
| 23 | Room-create report-cache staleness | `routers/rooms.py` |
| 1 | Access token lifetime 60× | `auth.py` |
| 5 | Duplicate username returns user | `routers/auth.py` |
| 6 | Booking grace window | `routers/bookings.py` |
| 10 | `start_time` overwritten | `routers/bookings.py` |
| 18 | Refund ledger truncation | `services/refunds.py` |
| 25 | Malformed datetime → 500 | `routers/bookings.py` |

**26 bugs** across the codebase, all fixed. Verified by `tests/test_full.py`
(52 tests) and the benchmark harness (80/80 criteria).
