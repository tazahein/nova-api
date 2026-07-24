# nova-api

FastAPI connection layer over the [nova_crm PostgreSQL database](https://github.com/tazahein/nova-crm-postgresql)..

The database repo builds the schema (contacts → leads → customers → orders); this repo exposes it as a JSON API.

**Live demo:** https://novatayza.duckdns.org ([interactive docs](https://novatayza.duckdns.org/docs))

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

## Run locally

Requires PostgreSQL running locally with the `nova_crm` database (build it from the [SQL repo](https://github.com/tazahein/nova-crm-postgresql)).

    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    uvicorn main:app --reload

Server runs at http://127.0.0.1:8000.

## Run with Docker

Requires PostgreSQL running on the host with the nova_crm database
(see Run locally).

Build the image:

    docker build -t nova-api .

Dependencies are installed in their own layer before the application
code is copied, so rebuilds after code-only changes reuse the cached
pip install and complete in seconds.

Run the container:

    docker run -d --name nova-api -p 8000:8000 \
      -e DATABASE_URL="host=host.docker.internal dbname=nova_crm user=<your-postgres-user>" \
      nova-api

Then visit http://localhost:8000/docs for the interactive API docs.

Notes:

- DATABASE_URL is read from the environment (see main.py). If it is
  not set, the app falls back to a local socket connection
  (dbname=nova_crm), so running outside Docker works unchanged.
- host.docker.internal is Docker Desktop's hostname for the host
  machine — localhost inside a container refers to the container
  itself, not the host.
- The username must be passed explicitly: TCP connections don't
  inherit your OS username the way local socket connections do.
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
