import os
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
