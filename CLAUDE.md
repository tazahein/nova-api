# CLAUDE.md — nova-api

Context for Claude Code sessions in this repo.

## Stack

FastAPI + PostgreSQL, containerised. `docker-compose.yml` runs an `api` service
(python:3.12-slim) and a `db` service (postgres:18) with a named volume. Config is
env-driven (`DATABASE_URL`), twelve-factor style. Auth is a single `X-API-Key`
header.

Note: FastAPI's `x_api_key` parameter maps to the HTTP header `x-api-key` —
underscores become hyphens. A mismatch here reads like an auth bug and isn't one.

Related repos: `nova-crm-postgresql` (schema, init scripts, SQL artefacts).

## Deployment model — read this before touching anything

**Edit locally → commit → the server pulls. Never edit on the server.**

The server's working tree must stay clean. If a change is needed in production, it
goes through this repo first. Do not suggest editing files over SSH, and do not
suggest `git stash`/`git checkout` on the server to work around drift — if the
server tree is dirty, that is a problem to report, not to resolve silently.

Deploy gotchas, all learned the hard way:

- On every server rebuild, verify `COPY main.py` is **not** `CACHED`. A cached
  layer ships the old code while every log line says success.
- Files created by tooling need `chmod 644` before being bind-mounted, or the
  container reads them as unreadable.
- The host runs UTC; containers run `Asia/Bangkok`. Every cron expression needs
  the 7-hour offset applied explicitly. Write the intended local time in a comment
  next to any cron line.
- Docker bypasses `ufw` via iptables DNAT. Ports are bound to `127.0.0.1` on
  purpose — do not "fix" a binding that looks unreachable from outside.
- `certbot` rewrites the nginx config after issuing a certificate. The live file is
  the source of truth, not the pre-certbot version.

## Working agreement

- **I am the commit gate.** Do not `git add`, `git commit`, `git push`, or create
  branches unless I ask in that message. Make the edits, show me `git diff`, stop.
- **No docker or deploy commands unless asked.** No `docker compose up`, no
  rebuilds, no restarts. Proposing one is fine; running it is not.
- **Scope is exactly what I listed.** No opportunistic refactors, no drive-by
  formatting, no "while I was in there". If you spot something else worth doing,
  say so at the end and let me decide.
- **No verification scripts.** Don't write throwaway scripts to prove a change
  worked unless I ask. Tell me what to check and I'll check it.
- **Structured files get edited with file tools, not text manipulation.** No
  `sed`/`awk`/`grep`-and-replace on YAML, JSON, or Markdown. Parse or edit properly.
- **Quote paths.** The parent directory name contains a space. Unquoted paths in
  bash commands will split and fail in confusing ways.

## Public-content rule

This repo is published. Any file, comment, README, or commit message must contain
**no curriculum, bootcamp, phase, week, or session references**. Keep technical
"why" comments and real project/database names (`nova-api`, `nova_crm`). If I paste
context that includes those references, strip them on the way in rather than
echoing them back into a file.

## Style

- Defensive JavaScript in n8n Code nodes: this build chokes on optional chaining
  and object spread. Write without both.
- Comments explain *why*, not *what*. The what is readable from the code.
- Prefer explicit over clever. This codebase gets read months later at 2am.

## Current state — August 2026

**`ai-email-lead-rescue`** — email triage and lead scoring, Gmail → AI classifier →
deterministic scoring → Telegram alert + auto-reply + Postgres log.
**Decommissioned as of 4 Aug 2026.** Feature-complete, workflow deactivated, kept as
a portfolio artefact. Two items closed as *decommissioned, not diagnosed*: a missing
`namesListing` definition in the classifier system prompt, and a poller that failed
to pick up a real inquiry on 31 Jul. Neither was ever root-caused. Do not describe
either as fixed.

**`cmw-booking-bot`** — spa booking bot, in progress. Backend endpoints live in this
repo (`/bookings`, `/bookings/lookup`). The n8n workflow is exported to
`workflows/cmw-booking-bot.json`. That export is a snapshot, not a live mirror — the
running workflow lives in the n8n SQLite volume, so re-export after editing in n8n
or the two drift apart silently.

The new-booking path is built and tested against stubs; cancel, reschedule, and
escalation paths are not built. Two nodes are stubs (`Classify Intent`, `Calendar
Events`) and their exact names are load-bearing — downstream code references
`$('Classify Intent')` directly.

**Open question that blocks design work:** the availability engine assumes one
calendar event means one therapist occupied. Staff meetings, room closures, and
holidays break that assumption. Unanswered by the client, which is why all-day
events currently escalate to a human rather than resolving.

**Known non-atomicity, and what actually backstops it:** the duplicate guard in n8n
is check-then-act, not transactional — two polls in the same minute can both read
zero and both book. The database backstop is the `no_therapist_overlap` constraint
on `bookings` (`EXCLUDE USING gist (therapist WITH =, tstzrange(starts_at, ends_at)
WITH &&) WHERE status = 'confirmed'`), which rejects overlapping confirmed bookings
for the same therapist atomically. Where it applies, that race cannot happen.

The gap is `therapist IS NULL`. Two NULLs do not compare equal, so NULL-therapist
rows never conflict with each other and the constraint never fires. v1 assigns no
therapist — `main.py` omits the column on both inserts — so **every row today is
NULL and the check-then-act race is fully live.** The constraint is protection for
the version that assigns therapists, not for the one running now. Accepted v1
limitation, not a bug to fix opportunistically.

## Ops trivia worth not rediscovering

- The Google OAuth client for the n8n project threw `401 invalid_client` in early
  August. It was fixed; **the fix method was not recorded.** If OAuth misbehaves
  again, that gap is the first thing to close.
- n8n: toggling Active is not the same as publishing a snapshot. Editor works but
  scheduled runs don't → suspect a stale published snapshot.
- n8n serves cached upstream output when you run a single node, so downstream nodes
  can read values from an earlier run. A node that looks fixed in isolation can
  still be broken on a full run — re-run the whole workflow before believing it.
- `POSTGRES_USER` is not declared in `docker-compose.yml`, so Postgres falls back to
  its default of `postgres`. The variable does not exist inside the container, so
  `psql -U "$POSTGRES_USER"` expands to an empty user and fails. Use `psql -U
  postgres` — the healthcheck and `DATABASE_URL` both hardcode it already.
- Postgres `SERIAL` ids do not reuse after `DELETE`. Gaps in id sequences are
  expected and not evidence of data loss.
