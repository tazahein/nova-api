# Booking database and n8n rollout

Use this runbook for an approved rollout of the booking changes in `nova-crm-postgresql` and `nova-api`. The order is **database → API → live n8n workflow → booking traffic**. The API uses the new database constraints; the exported workflow is only a reference for updating n8n.

This runbook is a plan. Running it requires a separate deployment decision. Keep the database backup, current live workflow revision, and previous API image available until verification is complete.

## Release gates

- [ ] Both repositories are available on the server at the reviewed revisions, and their server working trees are clean. Pull only through the normal repository deployment process. Do not edit server files or work around a dirty tree with stash or checkout.
- [ ] `python -m pytest tests -q` passes for `nova-api`; the migration and workflow paths have been exercised against a nonproduction database and calendar.
- [ ] The owner of booking traffic has chosen a maintenance window and can pause **all** booking writers, including the live n8n workflow and other API clients. Let running executions finish before the migration.
- [ ] The current published n8n workflow revision, live JSON export, settings, credentials used by each node, and calendar ID are recorded. Confirm how to republish the previous revision.
- [ ] A verified `nova_crm` database backup exists outside the repositories. Confirm enough space and a tested restore into an isolated database or instance.
- [ ] The owner accepts the remaining capacity risk: bookings for different clients can overlap while `therapist` is NULL. The n8n availability check is still the capacity control for those requests. All-day events and other calendar blocks still need human handling.

Stop if any gate fails. Do not send test bookings to real clients or use real client calendar entries for destructive tests.

## 1. Pause and back up

Pause every booking writer, then confirm no booking execution remains in flight. Record the current `bookings` row count and latest booking ID. Keep the old published workflow available for rollback.

From the `nova-api` checkout on the Linux host, set `BACKUP_FILE` to an approved secure path with a unique timestamp, then take a custom-format dump. The file is written on the host, outside the repository:

```bash
BACKUP_FILE="/secure/backups/nova_crm-pre-booking-YYYYMMDD-HHMM.dump"
docker compose exec -T db pg_dump -U postgres -d nova_crm -Fc > "$BACKUP_FILE"
test -s "$BACKUP_FILE"
docker compose exec -T db pg_restore -l < "$BACKUP_FILE"
```

Replace the example path and timestamp before running. A successful archive listing is only a format check; restore it into an isolated database or instance and confirm booking rows are readable before proceeding. Keep the backup access restricted.

## 2. Check existing data and migrate

For an **existing** Compose `nova_data` volume, SQL files in `/docker-entrypoint-initdb.d` do not run again. Run the one-time migration from the sibling database repository before starting the updated API. A **fresh** database instead gets the constraints from `05-bookings.sql`; do not run the migration on that fresh schema.

Run these checks against the paused production database. The extension query must return `btree_gist`; each conflict query must return zero rows. Record the row count before and after migration.

```sql
SELECT count(*) AS booking_rows FROM bookings;
SELECT extname FROM pg_extension WHERE extname = 'btree_gist';

SELECT calendar_event_id, array_agg(id ORDER BY id) AS booking_ids
FROM bookings GROUP BY calendar_event_id HAVING count(*) > 1;

SELECT supersedes_id, array_agg(id ORDER BY id) AS booking_ids
FROM bookings WHERE supersedes_id IS NOT NULL
GROUP BY supersedes_id HAVING count(*) > 1;

SELECT a.id AS first_id, b.id AS second_id
FROM bookings a JOIN bookings b ON a.id < b.id
  AND lower(a.client_email::text) = lower(b.client_email::text)
  AND tstzrange(a.starts_at, a.ends_at) && tstzrange(b.starts_at, b.ends_at)
WHERE a.status = 'confirmed' AND b.status = 'confirmed';
```

If a check finds conflicts, stop. Reconcile the database rows against the calendar and client history through a separately reviewed change; do not discard rows to make the migration pass.

Check the constraints before migration:

```sql
SELECT conname, contype
FROM pg_constraint
WHERE conrelid = 'bookings'::regclass
  AND conname IN (
    'bookings_calendar_event_id_key',
    'bookings_supersedes_id_key',
    'no_client_overlap'
  )
ORDER BY conname;
```

If all three constraints already exist with the expected types (`u`, `u`, `x`), record that the database is migrated and skip the one-time command. If only some exist, stop and investigate before changing the schema.

From the `nova-api` checkout, with `nova-crm-postgresql` as its sibling:

```bash
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U postgres -d nova_crm \
  < "../nova-crm-postgresql/migrations/001-booking-idempotency.sql"
```

The migration takes an exclusive lock on `bookings` and runs in one transaction. A failed precondition or SQL error rolls back its changes. Do not retry until the cause is understood. On success, verify the row count is unchanged, the constraint query returns exactly the three expected rows, and the three conflict queries still return zero rows. Keep booking writers paused if any verification differs.

## 3. Release and verify the API

After the database gate passes, deploy the reviewed `nova-api` revision through the normal image build and Compose process. Verify the build log shows `COPY main.py` executed rather than `CACHED`; otherwise the old application code may still be running. Do not change the loopback port binding or secrets as part of this rollout.

Verify against the deployed API:

1. `GET /` returns the expected health message, and the API container remains running with no new database errors.
2. `GET /bookings/by-calendar-event?event_id=...` returns 401 without a valid `X-API-Key`.
3. With the key, an unknown event ID returns `found: false` and null reference/status. A known historical event ID returns `found: true` with its booking reference and status. Use read-only checks on production data.
4. In the nonproduction environment, a repeated identical `POST /bookings` returns the original reference, a conflicting request returns 409, and concurrent attempts produce one booking. Confirm the database and calendar have no unintended duplicates.

Do not resume booking traffic yet.

## 4. Update the live n8n workflow

The committed `workflows/cmw-booking-bot.json` is **inactive** and contains a manual trigger, stub inputs, fixed test data, a placeholder API host, and a placeholder calendar ID. Do not import or activate it wholesale. Apply its booking response and reconciliation logic to the existing live workflow while preserving the live trigger, classifier, availability logic, credentials, API URL, and calendar ID.

In the live editor, check these paths and settings:

- `Create Booking` must return the full HTTP response and route HTTP errors for inspection. A 201 goes to the normal confirmation path.
- A 409 goes to `Find Event Booking`, which calls the API-key-protected event-ID lookup using the **newly created** calendar event ID. Only a valid `found: false` response may reach `Delete Rejected Event`.
- `found: true` keeps the calendar event and raises `Manual Review Required`. A timeout, 5xx, missing/invalid lookup response, or failed lookup also keeps the event and raises manual review.
- After confirmed deletion, `Conflict Needs Client Reply` raises a failed execution for staff follow-up. If deletion fails or its result is uncertain, treat the event as present until manually checked.
- Connect the failed executions to an n8n error workflow or ensure staff actively monitor them. Verify an owner receives and acts on the alert.

Exercise the **whole** workflow in a nonproduction copy with controlled records and calendar events. Cover 201 success, 409 with no linked booking, 409 with a linked historical booking, API timeout/5xx, lookup failure, and calendar deletion failure. Verify exactly one confirmation on success, no confirmation on a conflict, deletion only after a confirmed `found: false`, and an actionable failed execution for every manual path. Running a single node can reuse stale upstream data.

Save and **publish** the updated live workflow; changing the editor or Active toggle alone does not prove the published workflow changed. Run a controlled end-to-end check against the published revision, then export that live revision and compare the booking branches and settings with the reviewed design. Record the new published revision.

## 5. Resume and observe

Resume booking writers only when the database, API, and published n8n gates all pass. Watch the first real booking executions, API errors, database conflicts, n8n failed executions, and calendar/database event-ID matches. Assign every uncertain outcome to a person for reconciliation; do not automatically delete a calendar event after a timeout or ambiguous API response. Keep the backup and previous revisions until the monitoring window closes.

## Rollback and reconciliation

1. **Migration fails:** Its transaction rolls back. Leave writers paused, confirm the old API and workflow still operate against the unchanged schema, resolve the reported data or SQL issue, and repeat the backup and preflight checks before another attempt.
2. **API release fails after migration:** Pause writers. Restore the previous API image only after checking that its booking behavior is safe with the newly constrained database; otherwise keep traffic paused and fix forward. Preserve the database constraints and review any 409s or calendar events created during the failed rollout.
3. **Live n8n update fails:** Pause writers. Republish the recorded previous live workflow revision and confirm the **published** revision changed. Keep the new API and database constraints in place, then test the restored workflow with a controlled booking before resuming. Reconcile events from failed executions by event ID through the API lookup and the calendar; never infer that a timeout means no booking was written.
4. **Database restore is required:** Stop all writers first. Restore the verified backup into an isolated database and compare it with current bookings and calendar events. A production restore would erase bookings written after the dump; obtain a separate restore decision and a plan for those bookings before replacing production data. Restore a matching API/workflow combination and verify the complete booking path before reopening traffic.

References: [PostgreSQL locking](https://www.postgresql.org/docs/18/explicit-locking.html), [PostgreSQL backup archives](https://www.postgresql.org/docs/18/app-pgdump.html), [PostgreSQL restore](https://www.postgresql.org/docs/18/app-pgrestore.html), [n8n publishing](https://docs.n8n.io/source-control-environments/create-environments/), [n8n error workflows](https://docs.n8n.io/flow-logic/error-handling/).
