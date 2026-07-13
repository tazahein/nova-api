# nova-api

FastAPI connection layer over the [nova_crm PostgreSQL database](https://github.com/tazahein/nova-crm-postgresql)..

The database repo builds the schema (contacts → leads → customers → orders); this repo exposes it as a JSON API.

## Endpoints

| Method | Path | Returns |
|--------|------|---------|
| GET | `/` | Health check |
| GET | `/contacts` | All contacts, ordered by id |
| GET | `/customers/{customer_id}/orders` | One customer's orders, newest first. **404** if the customer doesn't exist |
| GET | `/portal/summary` | Per-customer order count + lifetime spend (three-table JOIN) |

Interactive docs with full response schemas at `/docs` once running.

## Design choices

- **Parameterized queries only** — values are passed via `%s` placeholders and a separate tuple, never string-formatted into SQL. Injection is impossible by construction.
- **Explicit 404 over 200 + empty list** — `/customers/{id}/orders` checks the customer exists before querying orders, so API consumers can distinguish "no orders yet" from "no such customer".
- **Pydantic response models** — every data endpoint declares its response shape. Output is validated, `Decimal` → `float` conversion is automatic, and `/docs` is self-documenting.
- **FastAPI's type-hinted path params** — `customer_id: int` rejects non-numeric input at the door before any code runs.

## Run it

Requires PostgreSQL running locally with the `nova_crm` database (build it from the [SQL repo](https://github.com/tazahein/nova-crm-postgresql)).

    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    uvicorn main:app --reload

Server runs at http://127.0.0.1:8000.

## Roadmap

- Typed `datetime` fields in response models
- Dockerize
- Cloud deployment
