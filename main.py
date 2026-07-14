import os
from fastapi import FastAPI, HTTPException
import psycopg

app = FastAPI()

DB = os.environ.get("DATABASE_URL", "dbname=nova_crm")

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
