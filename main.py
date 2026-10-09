import os
import secrets
from uuid import UUID, uuid4
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from typing import Literal
import psycopg

app = FastAPI()

DB = os.environ.get("DATABASE_URL", "dbname=nova_crm")

API_KEY = os.environ.get("NOVA_API_KEY")
BOOKING_CAPACITY = 4
BOOKING_TURNOVER = timedelta(minutes=15)
BOOKING_RECONCILIATION_ENABLED = os.environ.get("BOOKING_RECONCILIATION_ENABLED") == "true"


def require_api_key(x_api_key: str | None = Header(default=None)):
    # Fail closed on protected routes when the key is not configured.
    if not API_KEY or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


class InquiryIn(BaseModel):
    sender_email: str
    sender_name: str | None = None
    subject: str | None = None
    inquiry_text: str
    score: Literal["Hot", "Warm", "Cold"] | None = None
    has_budget_3m: bool | None = None
    has_timeline_3mo: bool | None = None
    names_listing: bool | None = None
    reply_text: str | None = None
    no_send_reason: str | None = None
    received_at: str | None = None  # ISO timestamp from Gmail; server now() if absent

    @model_validator(mode="after")
    def reply_xor_reason(self):
        # Exactly one of reply_text / no_send_reason must be set:
        # a row either got a reply or has a reason it didn't.
        if (self.reply_text is None) == (self.no_send_reason is None):
            raise ValueError("exactly one of reply_text or no_send_reason must be set")
        return self


class BookingIn(BaseModel):
    client_email: str
    client_name: str | None = None
    treatment: str
    starts_at: datetime
    ends_at: datetime
    calendar_event_id: str
    thread_id: str | None = None


class CancelIn(BaseModel):
    client_email: str


class RescheduleIn(BaseModel):
    client_email: str
    starts_at: datetime
    ends_at: datetime
    calendar_event_id: str


class ConfirmationOutcomeIn(BaseModel):
    state: Literal["confirmed", "uncertain"]
    gmail_message_id: str | None = Field(default=None, min_length=1, max_length=255)
    reason_code: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def require_outcome_evidence(self):
        if self.state == "confirmed" and not self.gmail_message_id:
            raise ValueError("confirmed outcome requires a Gmail message ID")
        if self.state == "uncertain" and not self.reason_code:
            raise ValueError("uncertain outcome requires a reason code")
        if self.state == "uncertain" and self.gmail_message_id:
            raise ValueError("uncertain outcome cannot include a Gmail message ID")
        return self


class ConfirmationReconciliationIn(BaseModel):
    state: Literal["confirmed", "failed"]
    actor: str = Field(min_length=2, max_length=100)
    note: str = Field(min_length=10, max_length=500)
    gmail_message_id: str | None = Field(default=None, min_length=1, max_length=255)
    non_delivery_verified: bool = False
    non_delivery_proof: Literal["before_gmail_invocation", "definitive_provider_rejection"] | None = None
    non_delivery_evidence: str | None = Field(default=None, min_length=20, max_length=500)

    @model_validator(mode="after")
    def require_confirmed_message(self):
        if self.state == "confirmed" and not self.gmail_message_id:
            raise ValueError("confirmed reconciliation requires a Gmail message ID")
        if self.state == "failed" and (
            not self.non_delivery_verified or not self.non_delivery_proof
            or not self.non_delivery_evidence
        ):
            raise ValueError("failed reconciliation requires verified non-delivery evidence")
        if self.state == "failed" and self.gmail_message_id:
            raise ValueError("failed reconciliation cannot include a Gmail message ID")
        return self


def _new_ref() -> str:
    # Random token, not a sequence: a guessable ref would let anyone
    # cancel a stranger's booking by incrementing a number.
    return f"CMW-{secrets.randbelow(9000) + 1000}-{secrets.token_hex(2)}"


# Clients reply to whichever confirmation they find first, which after a
# reschedule is often the superseded one. Walk the supersedes chain
# forward so an old ref still resolves to the live booking.
_RESOLVE = """
WITH RECURSIVE chain AS (
    SELECT id, booking_ref, client_email, treatment, starts_at, ends_at,
           calendar_event_id, status
    FROM bookings WHERE booking_ref = %s
  UNION ALL
    SELECT b.id, b.booking_ref, b.client_email, b.treatment, b.starts_at,
           b.ends_at, b.calendar_event_id, b.status
    FROM bookings b JOIN chain c ON b.supersedes_id = c.id
)
SELECT id, booking_ref, client_email, treatment, starts_at, ends_at,
       calendar_event_id, status
FROM chain ORDER BY (status = 'confirmed') DESC, id DESC LIMIT 1;
"""


_RESCHEDULE_REPLAY = """
WITH RECURSIVE chain AS (
    SELECT id FROM bookings WHERE booking_ref = %s
  UNION ALL
    SELECT b.id FROM bookings b JOIN chain c ON b.supersedes_id = c.id
)
SELECT b.booking_ref, previous.booking_ref, previous.calendar_event_id,
       b.client_email = %s AND b.starts_at = %s
       AND b.ends_at = %s AND b.status = 'confirmed'
       AND previous.id IN (SELECT id FROM chain)
FROM bookings b
LEFT JOIN bookings previous ON previous.id = b.supersedes_id
WHERE b.calendar_event_id = %s;
"""


_PEAK_BOOKINGS = """
WITH candidate AS (
    SELECT %s::timestamptz AS starts_at, %s::timestamptz AS ends_at,
           %s::interval AS turnover
), edges AS (
    SELECT edge.instant, edge.delta
    FROM candidate c
    JOIN bookings b ON b.status = 'confirmed'
      AND b.starts_at < c.ends_at + c.turnover
      AND b.ends_at + c.turnover > c.starts_at
    CROSS JOIN LATERAL (VALUES
        (GREATEST(b.starts_at, c.starts_at), 1),
        (LEAST(b.ends_at + c.turnover, c.ends_at + c.turnover), -1)
    ) AS edge(instant, delta)
), changes AS (
    SELECT instant, SUM(delta) AS delta FROM edges GROUP BY instant
), occupancy AS (
    SELECT SUM(delta) OVER (ORDER BY instant) AS booked FROM changes
)
SELECT COALESCE(MAX(booked), 0) FROM occupancy;
"""


def _lock_booking_writes(cur):
    # Serialize the capacity check with all booking table writers, including
    # other API workers. Ordinary reads can continue during the short lock.
    cur.execute("LOCK TABLE bookings IN SHARE ROW EXCLUSIVE MODE;")


def _check_booking_capacity(cur, starts_at: datetime, ends_at: datetime):
    # Counting every overlapping row would reject valid staggered bookings.
    # The peak count uses the same turnover window as the calendar check.
    cur.execute(_PEAK_BOOKINGS, (starts_at, ends_at, BOOKING_TURNOVER))
    if cur.fetchone()[0] >= BOOKING_CAPACITY:
        raise HTTPException(status_code=409, detail="No booking capacity at requested time")


def _replayed_reschedule(cur, ref: str, body: RescheduleIn):
    cur.execute(
        _RESCHEDULE_REPLAY,
        (ref, body.client_email, body.starts_at, body.ends_at,
         body.calendar_event_id),
    )
    row = cur.fetchone()
    if row is None:
        return None
    if row[3] is not True:
        raise HTTPException(status_code=409, detail="Calendar event conflicts with a booking")
    return {"booking_ref": row[0], "previous_ref": row[1],
            "old_calendar_event_id": row[2]}


@app.get("/")
def home():
    return {"message": "nova-api is alive"}

@app.get("/contacts", dependencies=[Depends(require_api_key)])
def list_contacts():
    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, email "
                "FROM contacts ORDER BY id;"
            )
            rows = cur.fetchall()
    return {
        "contacts": [
            {"id": r[0], "name": r[1], "email": r[2]}
            for r in rows
        ]
    }

@app.get("/customers/{customer_id}/orders", dependencies=[Depends(require_api_key)])
def customer_orders(customer_id: int):
    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM customers WHERE id = %s;",
                (customer_id,)
            )
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="Customer not found")
            cur.execute(
                "SELECT id, amount, status, created_at "
                "FROM orders WHERE customer_id = %s "
                "ORDER BY created_at DESC;",
                (customer_id,)
            )
            rows = cur.fetchall()
    return {
        "customer_id": customer_id,
        "orders": [
            {"id": r[0], "amount": float(r[1]), "status": r[2], "created_at": str(r[3])}
            for r in rows
        ]
    }

@app.get("/portal/summary", dependencies=[Depends(require_api_key)])
def portal_summary():
    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.id, ct.name, COUNT(o.id), COALESCE(SUM(o.amount), 0) "
                "FROM customers c "
                "JOIN contacts ct ON ct.id = c.contact_id "
                "LEFT JOIN orders o ON o.customer_id = c.id "
                "GROUP BY c.id, ct.name ORDER BY SUM(o.amount) DESC NULLS LAST;"
            )
            rows = cur.fetchall()
    return {
        "summary": [
            {"customer_id": r[0], "name": r[1], "order_count": r[2], "lifetime_spend": float(r[3])}
            for r in rows
        ]
    }


@app.get("/inquiries/dedupe", dependencies=[Depends(require_api_key)])
def inquiries_dedupe(sender: str):
    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT max(reply_sent_at) FROM inquiries "
                "WHERE sender_email = %s "
                "AND reply_sent_at >= date_trunc('day', now() AT TIME ZONE 'Asia/Bangkok') "
                "AT TIME ZONE 'Asia/Bangkok';",
                (sender,),
            )
            last = cur.fetchone()[0]
    return {
        "already_replied_today": last is not None,
        "last_reply_at": str(last) if last else None,
    }


@app.post("/inquiries", status_code=201, dependencies=[Depends(require_api_key)])
def create_inquiry(inq: InquiryIn):
    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inquiries "
                "(sender_email, sender_name, subject, inquiry_text, score, "
                " has_budget_3m, has_timeline_3mo, names_listing, "
                " reply_text, no_send_reason, reply_sent_at, received_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                " CASE WHEN %s::text IS NOT NULL THEN now() END, "
                " COALESCE(%s::timestamptz, now())) "
                "RETURNING id, received_at;",
                (
                    inq.sender_email, inq.sender_name, inq.subject,
                    inq.inquiry_text, inq.score,
                    inq.has_budget_3m, inq.has_timeline_3mo, inq.names_listing,
                    inq.reply_text, inq.no_send_reason,
                    inq.reply_text, inq.received_at,
                ),
            )
            row = cur.fetchone()
    return {"id": row[0], "received_at": str(row[1])}


@app.post("/bookings", status_code=201, dependencies=[Depends(require_api_key)])
def create_booking(b: BookingIn):
    # Calendar event is created by the workflow first, so its ID exists
    # here. A replay returns 200 so the workflow does not send twice.
    ref = _new_ref()
    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:
            _lock_booking_writes(cur)
            cur.execute(
                "SELECT id, booking_ref FROM bookings "
                "WHERE calendar_event_id = %s AND client_email = %s "
                "AND client_name IS NOT DISTINCT FROM %s "
                "AND treatment = %s AND starts_at = %s AND ends_at = %s "
                "AND thread_id IS NOT DISTINCT FROM %s "
                "AND supersedes_id IS NULL AND status = 'confirmed';",
                (b.calendar_event_id, b.client_email, b.client_name,
                 b.treatment, b.starts_at, b.ends_at, b.thread_id),
            )
            replay = cur.fetchone()
            if replay is not None:
                return JSONResponse(status_code=200, content={
                    "id": replay[0], "booking_ref": replay[1]
                })
            _check_booking_capacity(cur, b.starts_at, b.ends_at)
            cur.execute(
                "INSERT INTO bookings "
                "(booking_ref, client_email, client_name, treatment, "
                " starts_at, ends_at, calendar_event_id, thread_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING RETURNING id, booking_ref;",
                (ref, b.client_email, b.client_name, b.treatment,
                 b.starts_at, b.ends_at, b.calendar_event_id, b.thread_id),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(
                    status_code=409, detail="Booking conflicts with an existing booking"
                )
    return {"id": row[0], "booking_ref": row[1]}


@app.post("/bookings/{ref}/confirmation-claim", status_code=201,
          dependencies=[Depends(require_api_key)])
def claim_booking_confirmation(ref: str):
    # The booking row lock and unique attempt key serialize concurrent runs.
    # No automatic path can claim a second attempt, even after a failure.
    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, confirmation_claimed_at FROM bookings "
                "WHERE booking_ref = %s FOR UPDATE;",
                (ref,),
            )
            booking = cur.fetchone()
            if booking is None or booking[1] != "confirmed":
                raise HTTPException(status_code=404, detail="Booking not found")
            cur.execute(
                "SELECT id, state, gmail_message_id FROM booking_confirmation_attempts "
                "WHERE booking_id = %s FOR UPDATE;",
                (booking[0],),
            )
            attempt = cur.fetchone()
            if attempt is not None:
                state = _expire_pending_attempt(cur, attempt[0], attempt[1])
                return JSONResponse(status_code=200, content={
                    "booking_ref": ref, "claimed": False,
                    "attempt_id": str(attempt[0]), "state": state,
                })

            attempt_id = uuid4()
            if booking[2] is not None:
                # Fail closed if an older claim lacks an attempt row.
                state, action, actor = "uncertain", "legacy_claim", "migration"
                claimed_at = booking[2]
            else:
                state, action, actor = "pending", "claim", "workflow"
                claimed_at = None
                cur.execute(
                    "UPDATE bookings SET confirmation_claimed_at = now() "
                    "WHERE id = %s;",
                    (booking[0],),
                )
            cur.execute(
                "INSERT INTO booking_confirmation_attempts "
                "(id, booking_id, state, claimed_at) "
                "VALUES (%s, %s, %s, COALESCE(%s, now()));",
                (attempt_id, booking[0], state, claimed_at),
            )
            _record_confirmation_event(cur, attempt_id, None, state,
                                       action, actor, None, None)
    response = {
        "booking_ref": ref, "claimed": state == "pending",
        "attempt_id": str(attempt_id), "state": state,
    }
    return response if state == "pending" else JSONResponse(status_code=200, content=response)


def _record_confirmation_event(cur, attempt_id, old_state, new_state,
                               action, actor, note, gmail_message_id):
    cur.execute(
        "INSERT INTO booking_confirmation_events "
        "(attempt_id, from_state, to_state, action, actor, note, gmail_message_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s);",
        (attempt_id, old_state, new_state, action, actor, note, gmail_message_id),
    )


def _expire_pending_attempt(cur, attempt_id, state):
    if state != "pending":
        return state
    cur.execute(
        "UPDATE booking_confirmation_attempts "
        "SET state = 'uncertain', changed_at = now() "
        "WHERE id = %s AND state = 'pending' "
        "AND claimed_at < now() - interval '2 minutes' RETURNING id;",
        (attempt_id,),
    )
    if cur.fetchone() is None:
        return state
    _record_confirmation_event(cur, attempt_id, "pending", "uncertain",
                               "timeout", "system", "No outcome within two minutes", None)
    return "uncertain"


@app.get("/bookings/{ref}/confirmation-attempt",
         dependencies=[Depends(require_api_key)])
def get_booking_confirmation_attempt(ref: str):
    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT a.id, a.state, a.gmail_message_id "
                "FROM booking_confirmation_attempts a "
                "JOIN bookings b ON b.id = a.booking_id "
                "WHERE b.booking_ref = %s FOR UPDATE OF a;",
                (ref,),
            )
            attempt = cur.fetchone()
            if attempt is None:
                raise HTTPException(status_code=404, detail="Confirmation attempt not found")
            state = _expire_pending_attempt(cur, attempt[0], attempt[1])
            cur.execute(
                "SELECT from_state, to_state, action, actor, note, "
                "gmail_message_id, recorded_at FROM booking_confirmation_events "
                "WHERE attempt_id = %s ORDER BY id;",
                (attempt[0],),
            )
            history = [
                {"from_state": row[0], "to_state": row[1], "action": row[2],
                 "actor": row[3], "note": row[4], "gmail_message_id": row[5],
                 "recorded_at": row[6].isoformat()}
                for row in cur.fetchall()
            ]
    return {"booking_ref": ref, "attempt_id": str(attempt[0]),
            "state": state, "gmail_message_id": attempt[2], "history": history}


def _transition_confirmation_attempt(ref, attempt_id, new_state, action,
                                     actor, note, gmail_message_id, manual):
    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT a.state, a.gmail_message_id "
                "FROM booking_confirmation_attempts a "
                "JOIN bookings b ON b.id = a.booking_id "
                "WHERE b.booking_ref = %s AND a.id = %s FOR UPDATE OF a;",
                (ref, attempt_id),
            )
            previous = cur.fetchone()
            if previous is None:
                raise HTTPException(status_code=404, detail="Confirmation attempt not found")
            old_state, old_message_id = previous
            if old_state == new_state and old_message_id == gmail_message_id:
                return JSONResponse(status_code=200, content={
                    "booking_ref": ref, "attempt_id": str(attempt_id), "state": new_state,
                })
            if manual:
                allowed = (new_state == "failed" and old_state == "uncertain"
                           and old_message_id is None) or (
                    new_state == "confirmed" and old_state in ("pending", "uncertain", "failed")
                )
            else:
                allowed = old_state in ("pending", "uncertain")
            if not allowed:
                raise HTTPException(status_code=409, detail="Attempt is already resolved")
            cur.execute(
                "UPDATE booking_confirmation_attempts "
                "SET state = %s, gmail_message_id = COALESCE(%s, gmail_message_id), "
                "changed_at = now() WHERE id = %s;",
                (new_state, gmail_message_id, attempt_id),
            )
            _record_confirmation_event(cur, attempt_id, old_state, new_state,
                                       action, actor, note, gmail_message_id)
    return {"booking_ref": ref, "attempt_id": str(attempt_id), "state": new_state}


@app.post("/bookings/{ref}/confirmation-attempts/{attempt_id}/outcome",
          dependencies=[Depends(require_api_key)])
def record_booking_confirmation_outcome(ref: str, attempt_id: UUID,
                                        outcome: ConfirmationOutcomeIn):
    return _transition_confirmation_attempt(
        ref, attempt_id, outcome.state, "workflow_" + outcome.state,
        "workflow", outcome.reason_code, outcome.gmail_message_id, False,
    )


@app.post("/bookings/{ref}/confirmation-attempts/{attempt_id}/reconcile",
          dependencies=[Depends(require_api_key)])
def reconcile_booking_confirmation(ref: str, attempt_id: UUID,
                                   decision: ConfirmationReconciliationIn):
    if not BOOKING_RECONCILIATION_ENABLED:
        raise HTTPException(status_code=403, detail="Manual reconciliation is disabled")
    note = decision.note
    if decision.state == "failed":
        note += (" | Verified non-delivery (" + decision.non_delivery_proof
                 + "): " + decision.non_delivery_evidence)
    return _transition_confirmation_attempt(
        ref, attempt_id, decision.state, "reconciled_" + decision.state,
        decision.actor, note, decision.gmail_message_id, True,
    )


@app.get("/bookings/lookup", dependencies=[Depends(require_api_key)])
def lookup_bookings(email: str, ref: str | None = None):
    # Returns a count the workflow switches on: 0 -> escalate,
    # 1 -> act, many -> ask the client which. Never guesses.
    # Ownership lives in this WHERE clause, not in the AI prompt.
    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:
            if ref:
                cur.execute(_RESOLVE, (ref,))
                row = cur.fetchone()
                # Wrong sender is reported as not-found, not as
                # "exists but denied" — no probing for other people's refs.
                if (row is None or row[2].lower() != email.lower()
                        or row[7] != "confirmed"):
                    return {"count": 0, "bookings": []}
                rows = [row]
            else:
                cur.execute(
                    "SELECT id, booking_ref, client_email, treatment, "
                    "starts_at, ends_at, calendar_event_id, status "
                    "FROM bookings "
                    "WHERE client_email = %s AND status = 'confirmed' "
                    "AND starts_at > now() ORDER BY starts_at;",
                    (email,),
                )
                rows = cur.fetchall()
    return {
        "count": len(rows),
        "bookings": [
            {"booking_ref": r[1], "treatment": r[3],
             "starts_at": str(r[4]), "ends_at": str(r[5]),
             "calendar_event_id": r[6]}
            for r in rows
        ],
    }


@app.get("/bookings/by-calendar-event", dependencies=[Depends(require_api_key)])
def booking_by_calendar_event(event_id: str):
    # Reconciliation must include historical rows: an event linked to any
    # booking is never safe for the workflow to delete automatically.
    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT booking_ref, status FROM bookings "
                "WHERE calendar_event_id = %s;",
                (event_id,),
            )
            row = cur.fetchone()
    return {
        "found": row is not None,
        "booking_ref": row[0] if row else None,
        "status": row[1] if row else None,
    }


@app.patch("/bookings/{ref}/cancel", dependencies=[Depends(require_api_key)])
def cancel_booking(ref: str, body: CancelIn):
    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:
            cur.execute(_RESOLVE, (ref,))
            row = cur.fetchone()
            if row is None or row[2].lower() != body.client_email.lower():
                raise HTTPException(status_code=404, detail="Booking not found")
            if row[7] == "cancelled":
                # Idempotent: a duplicate poll must not 500. The workflow
                # can safely re-run without a second apology email.
                return {"booking_ref": row[1],
                        "calendar_event_id": row[6],
                        "already_cancelled": True}
            cur.execute(
                "UPDATE bookings SET status = 'cancelled' WHERE id = %s;",
                (row[0],),
            )
    # Event ID handed back so the workflow deletes the calendar entry.
    # If that delete fails we get a blocked slot, never a double-booking.
    return {"booking_ref": row[1],
            "calendar_event_id": row[6],
            "already_cancelled": False}


@app.post("/bookings/{ref}/reschedule", dependencies=[Depends(require_api_key)])
def reschedule_booking(ref: str, body: RescheduleIn):
    # Called only after the NEW calendar event exists and is confirmed.
    # Claim the current row before creating its successor, in one transaction.
    new_ref = _new_ref()
    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:
            _lock_booking_writes(cur)
            replay = _replayed_reschedule(cur, ref, body)
            if replay is not None:
                return replay
            cur.execute(_RESOLVE, (ref,))
            old = cur.fetchone()
            if (old is None or old[2].lower() != body.client_email.lower()
                    or old[7] != "confirmed"):
                raise HTTPException(status_code=404, detail="Booking not found")
            cur.execute(
                "UPDATE bookings SET status = 'superseded' "
                "WHERE id = %s AND status = 'confirmed' RETURNING id;",
                (old[0],),
            )
            if cur.fetchone() is None:
                replay = _replayed_reschedule(cur, ref, body)
                if replay is not None:
                    return replay
                raise HTTPException(
                    status_code=409, detail="Booking was changed by another request"
                )
            _check_booking_capacity(cur, body.starts_at, body.ends_at)
            cur.execute(
                "INSERT INTO bookings "
                "(booking_ref, client_email, client_name, treatment, "
                " starts_at, ends_at, calendar_event_id, thread_id, "
                " supersedes_id) "
                "SELECT %s, client_email, client_name, treatment, "
                " %s, %s, %s, thread_id, id "
                "FROM bookings WHERE id = %s "
                "ON CONFLICT DO NOTHING RETURNING booking_ref;",
                (new_ref, body.starts_at, body.ends_at,
                 body.calendar_event_id, old[0]),
            )
            created = cur.fetchone()
            if created is None:
                raise HTTPException(
                    status_code=409, detail="Booking conflicts with an existing booking"
                )
    return {"booking_ref": created[0],
            "previous_ref": old[1],
            "old_calendar_event_id": old[6]}
