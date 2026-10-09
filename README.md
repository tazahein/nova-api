# nova-api

FastAPI connection layer over the [nova_crm PostgreSQL database](https://github.com/tazahein/nova-crm-postgresql)..

The database repo builds the schema (contacts → leads → customers → orders); this repo exposes it as a JSON API.

**Public demo:** Currently unavailable. Run the API locally to use its interactive docs at `/docs`.

## Endpoints

| Method | Path | Returns |
|--------|------|---------|
| GET | `/` | Health check |
| GET | `/contacts` | All contacts, ordered by id |
| GET | `/customers/{customer_id}/orders` | One customer's orders, newest first. **404** if the customer doesn't exist |
| GET | `/portal/summary` | Per-customer order count + lifetime spend (three-table JOIN) |
| GET | `/bookings/by-calendar-event?event_id=...` | API key protected reconciliation lookup. Returns whether any booking, including historical rows, references the event, plus its reference and status. |
| POST | `/bookings/{ref}/confirmation-claim` | API key protected one-time claim before attempting to send a confirmation. Returns 201 only for the first claim; repeats return 200. |

Interactive docs with full response schemas at `/docs` once running.

## Design choices

- **Parameterized queries only** — values are passed via `%s` placeholders and a separate tuple, never string-formatted into SQL. Injection is impossible by construction.
- **Explicit 404 over 200 + empty list** — `/customers/{id}/orders` checks the customer exists before querying orders, so API consumers can distinguish "no orders yet" from "no such customer".
- **Pydantic response models** — every data endpoint declares its response shape. Output is validated, `Decimal` → `float` conversion is automatic, and `/docs` is self-documenting.
- **FastAPI's type-hinted path params** — `customer_id: int` rejects non-numeric input at the door before any code runs.

## Configuration

Configuration values:

| Variable | Read by | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | API | PostgreSQL connection string. If unset, the app uses `dbname=nova_crm` and libpq connection defaults; set it explicitly for TCP connections. |
| `NOVA_API_KEY` | API | Key clients send in the `X-API-Key` header for every database-backed endpoint. If unset or empty, those endpoints return 401. |
| `POSTGRES_PASSWORD` | Docker Compose | Password for PostgreSQL; Compose also puts it in the API's `DATABASE_URL`. |
| `BOOKING_RECONCILIATION_ENABLED` | API | Defaults to `false`. Set to `true` only when an operator is authorized to make documented manual confirmation decisions. |

The health endpoint and API documentation are public; all database-backed endpoints require an API key. The app does not load `.env` itself; direct Python runs need environment variables set in the shell.

Booking creation and rescheduling use `calendar_event_id` to recognize a repeated request. A new booking returns HTTP 201; an identical creation replay returns the original reference with HTTP 200. Reuse with different details or a conflicting booking returns HTTP 409. API booking writes also serialize a capacity check: at most four confirmed bookings may overlap, including 15 minutes of turnover. Apply migrations `001-booking-idempotency.sql`, `002-booking-confirmation-claim.sql`, and `003-booking-confirmation-attempts.sql` in order from the sibling `nova-crm-postgresql` repository before running this API version. New databases get the schema from `05-bookings.sql`.

The exported booking workflow confirms only a new HTTP 201 booking after a separate one-time confirmation claim also returns 201. A replayed booking or claim response goes to manual review so staff can check whether confirmation was already attempted. A claim is recorded before sending and does not prove email delivery; uncertain sends require manual reconciliation. Directly restarting at the Gmail Send node bypasses the claim; reconcile delivery before any manual resend. A failed decision requires an uncertain attempt and documented proof that Gmail was never invoked or definitively rejected the send (`non_delivery_verified`, `non_delivery_proof`, and `non_delivery_evidence`); an empty Sent folder or inbox alone is not proof. Otherwise leave the attempt uncertain. Manual reconciliation is disabled unless `BOOKING_RECONCILIATION_ENABLED=true` is explicitly set on the API. After a 409, the workflow checks the event ID and deletes the newly created calendar event only when the API confirms that no booking references it. On a timeout, failed lookup, or unexpected response, it keeps the event and fails the execution for manual reconciliation. A successful cleanup also fails the execution so staff can contact the client. This export is an inactive snapshot; update and test the live n8n workflow separately. Configure an n8n error workflow or monitor failed executions so manual reviews are noticed.

For Docker Compose, copy `.env.example` to `.env` in this directory and set both values. Compose reads them for interpolation and passes the resulting values to the containers. Keep `.env` private (`.gitignore` excludes it). The Compose file also needs `nova-crm-postgresql` as a sibling directory for its SQL initialization files.

## Run locally

Requires PostgreSQL running locally with the `nova_crm` database (build it from the [SQL repo](https://github.com/tazahein/nova-crm-postgresql)).

On Windows PowerShell:

    py -3.12 -m venv venv
    .\venv\Scripts\python.exe -m pip install -r requirements.txt
    $env:DATABASE_URL = "host=localhost dbname=nova_crm user=postgres password=<db-password>"
    $env:NOVA_API_KEY = "<long-random-key>"
    .\venv\Scripts\python.exe -m uvicorn main:app --reload

On macOS/Linux:

    python3.12 -m venv venv
    venv/bin/python -m pip install -r requirements.txt
    export DATABASE_URL="host=localhost dbname=nova_crm user=postgres password=<db-password>"
    export NOVA_API_KEY="<long-random-key>"
    venv/bin/python -m uvicorn main:app --reload

Server runs at http://127.0.0.1:8000.

## Run tests

The API contract tests run without a PostgreSQL server:

    python -m pip install -r requirements-dev.txt
    python -m pytest tests -q

## Run with Docker

Requires PostgreSQL running on the host with the nova_crm database
(see Run locally).

Build the image:

    docker build -t nova-api .

Dependencies are installed in their own layer before the application
code is copied, so rebuilds after code-only changes reuse the cached
pip install and complete in seconds.

Set `DATABASE_URL` in your shell with `host=host.docker.internal` instead of `host=localhost`, and set `NOVA_API_KEY` there too. Then pass both to the container (the example uses a POSIX shell):

    docker run -d --name nova-api -p 8000:8000 \
      -e DATABASE_URL -e NOVA_API_KEY \
      nova-api

Then visit http://localhost:8000/docs for the interactive API docs.

Notes:

- `host.docker.internal` is Docker Desktop's hostname for the host
  machine — localhost inside a container refers to the container
  itself, not the host.
- Include a database user and password in `DATABASE_URL` when PostgreSQL
  requires them. `-e NAME` forwards that variable from the host shell.
- The server binds to 0.0.0.0 inside the container; binding to
  127.0.0.1 would make it unreachable from the host even with -p.

Stop and remove:

    docker rm -f nova-api

## Roadmap

- Typed `datetime` fields in response models
- ~~Dockerize~~ ✅ done — see Run with Docker
- ~~Cloud deployment~~ ✅ done — see Deployment

## Deployment

Runs on a cloud VPS as a Docker Compose stack (see `docker-compose.yml`)
behind an Nginx reverse proxy with Let's Encrypt HTTPS. The API container
binds to loopback only; Nginx is the sole public entry point. The proxy
config and a restore guide live in [`deploy/nginx/`](deploy/nginx/).
The booking database migration and live n8n update have a
[deployment runbook](deploy/booking-rollout.md) with verification and rollback gates.
