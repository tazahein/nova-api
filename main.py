import os
import secrets
from datetime import datetime
from fastapi import FastAPI, HTTPException, Depends, Header
from pydantic import BaseModel, model_validator
from typing import Literal
import psycopg

app = FastAPI()

DB = os.environ.get("DATABASE_URL", "dbname=nova_crm")

API_KEY = os.environ.get("NOVA_API_KEY")


def require_api_key(x_api_key: str | None = Header(default=None)):
    # Fail CLOSED: if the env var is missing, everything 401s.
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


@app.get("/")
def home():
    return {"message": "nova-api is alive"}

@app.get("/contacts")
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

@app.get("/customers/{customer_id}/orders")
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

@app.get("/portal/summary")
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
    # here. If this insert fails we orphan a calendar event (a blocked
    # slot) rather than lose a confirmed booking.
    ref = _new_ref()
    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bookings "
                "(booking_ref, client_email, client_name, treatment, "
                " starts_at, ends_at, calendar_event_id, thread_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id, booking_ref;",
                (ref, b.client_email, b.client_name, b.treatment,
                 b.starts_at, b.ends_at, b.calendar_event_id, b.thread_id),
            )
            row = cur.fetchone()
    return {"id": row[0], "booking_ref": row[1]}


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
    # Insert-then-supersede in one transaction: both or neither.
    new_ref = _new_ref()
    with psycopg.connect(DB) as conn:
        with conn.cursor() as cur:
            cur.execute(_RESOLVE, (ref,))
            old = cur.fetchone()
            if (old is None or old[2].lower() != body.client_email.lower()
                    or old[7] != "confirmed"):
                raise HTTPException(status_code=404, detail="Booking not found")
            cur.execute(
                "INSERT INTO bookings "
                "(booking_ref, client_email, client_name, treatment, "
                " starts_at, ends_at, calendar_event_id, thread_id, "
                " supersedes_id) "
                "SELECT %s, client_email, client_name, treatment, "
                " %s, %s, %s, thread_id, id "
                "FROM bookings WHERE id = %s RETURNING booking_ref;",
                (new_ref, body.starts_at, body.ends_at,
                 body.calendar_event_id, old[0]),
            )
            created = cur.fetchone()[0]
            cur.execute(
                "UPDATE bookings SET status = 'superseded' WHERE id = %s;",
                (old[0],),
            )
    return {"booking_ref": created,
            "previous_ref": old[1],
            "old_calendar_event_id": old[6]}