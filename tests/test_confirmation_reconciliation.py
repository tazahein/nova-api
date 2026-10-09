"""Focused, database-free tests for the confirmation ledger state machine."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier, Lock
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import main


ATTEMPT_ID = UUID("00000000-0000-0000-0000-000000000001")
REF = "CMW-synthetic"
ATTEMPT_URL = f"/bookings/{REF}/confirmation-attempts/{ATTEMPT_ID}"
HEADERS = {"x-api-key": "test-api-key"}
FAILED_DECISION = {
    "state": "failed", "actor": "staging reviewer",
    "note": "Checked the synthetic test delivery outcome",
    "non_delivery_verified": True,
    "non_delivery_proof": "before_gmail_invocation",
    "non_delivery_evidence": "Execution stopped at the confirmation claim before Gmail was invoked",
}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "test-api-key")
    monkeypatch.setattr(main, "BOOKING_RECONCILIATION_ENABLED", True)
    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    monkeypatch.setattr(main.psycopg, "connect", MagicMock(return_value=connection))
    return TestClient(main.app), connection, cursor


@pytest.mark.parametrize("expired", [False, True])
def test_pending_timeout_is_audited_and_never_reclaimed(api, expired):
    client, _, cursor = api
    cursor.fetchone.side_effect = [
        (7, "confirmed", datetime(2026, 10, 8, tzinfo=timezone.utc)),
        (ATTEMPT_ID, "pending", None),
        (ATTEMPT_ID,) if expired else None,
    ]
    response = client.post(f"/bookings/{REF}/confirmation-claim", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["claimed"] is False
    assert response.json()["state"] == ("uncertain" if expired else "pending")
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert any("claimed_at < now() - interval '2 minutes'" in sql for sql in statements)
    assert not any("INSERT INTO booking_confirmation_attempts" in sql for sql in statements)
    events = [call for call in cursor.execute.call_args_list
              if "INSERT INTO booking_confirmation_events" in call.args[0]]
    assert len(events) == int(expired)
    if expired:
        assert events[0].args[1][1:5] == ("pending", "uncertain", "timeout", "system")


@pytest.mark.parametrize(
    "old_state,new_state,manual,expected",
    [
        ("pending", "confirmed", False, 200),
        ("pending", "uncertain", False, 200),
        ("uncertain", "confirmed", False, 200),
        ("uncertain", "failed", True, 200),
        ("pending", "failed", True, 409),
        ("confirmed", "failed", True, 409),
        ("failed", "uncertain", False, 409),
        ("confirmed", "uncertain", False, 409),
        ("failed", "confirmed", True, 200),
    ],
)
def test_legal_and_illegal_transitions_are_audited(
    api, old_state, new_state, manual, expected
):
    client, connection, cursor = api
    cursor.fetchone.return_value = (old_state, None)
    if manual:
        payload = (dict(FAILED_DECISION) if new_state == "failed" else {
            "state": "confirmed", "actor": "staging reviewer",
            "note": "Located the previously delivered synthetic test message",
            "gmail_message_id": "synthetic-gmail-id",
        })
        path = f"{ATTEMPT_URL}/reconcile"
    else:
        payload = ({"state": "confirmed", "gmail_message_id": "synthetic-gmail-id"}
                   if new_state == "confirmed" else
                   {"state": "uncertain", "reason_code": "gmail_timeout"})
        path = f"{ATTEMPT_URL}/outcome"
    response = client.post(path, json=payload, headers=HEADERS)
    assert response.status_code == expected
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert "FOR UPDATE OF a" in statements[0]
    events = [call for call in cursor.execute.call_args_list
              if "INSERT INTO booking_confirmation_events" in call.args[0]]
    assert len(events) == int(expected == 200)
    if expected == 200:
        assert events[0].args[1][1:4] == (old_state, new_state,
                                         ("reconciled_" if manual else "workflow_") + new_state)
        if new_state == "failed":
            assert FAILED_DECISION["non_delivery_evidence"] in events[0].args[1][5]
    else:
        assert not any("UPDATE booking_confirmation_attempts" in sql for sql in statements)
        assert connection.__exit__.call_args.args[0] is main.HTTPException


@pytest.mark.parametrize("change", [
    {"non_delivery_verified": False},
    {"non_delivery_proof": None},
    {"non_delivery_evidence": None},
    {"gmail_message_id": "synthetic-gmail-id"},
])
def test_failed_reconciliation_requires_consistent_evidence(api, change):
    client, _, cursor = api
    response = client.post(f"{ATTEMPT_URL}/reconcile",
                           json={**FAILED_DECISION, **change}, headers=HEADERS)
    assert response.status_code == 422
    cursor.execute.assert_not_called()


@pytest.mark.parametrize("payload", [
    {"state": "uncertain", "reason_code": "gmail_timeout",
     "gmail_message_id": "synthetic-gmail-id"},
    {"state": "uncertain"},
    {"state": "confirmed"},
])
def test_workflow_outcome_rejects_ambiguous_evidence(api, payload):
    client, _, cursor = api
    response = client.post(f"{ATTEMPT_URL}/outcome", json=payload, headers=HEADERS)
    assert response.status_code == 422
    cursor.execute.assert_not_called()


def test_known_gmail_id_cannot_be_marked_failed(api):
    client, _, cursor = api
    cursor.fetchone.return_value = ("uncertain", "synthetic-gmail-id")
    response = client.post(f"{ATTEMPT_URL}/reconcile",
                           json=FAILED_DECISION, headers=HEADERS)
    assert response.status_code == 409
    assert cursor.execute.call_count == 1


def test_manual_reconciliation_is_disabled_without_explicit_flag(api, monkeypatch):
    client, _, cursor = api
    monkeypatch.setattr(main, "BOOKING_RECONCILIATION_ENABLED", False)
    response = client.post(f"{ATTEMPT_URL}/reconcile",
                           json=FAILED_DECISION, headers=HEADERS)
    assert response.status_code == 403
    cursor.execute.assert_not_called()


def test_unresolved_attempt_stays_uncertain_and_history_is_ordered(api):
    client, _, cursor = api
    recorded_at = datetime(2026, 10, 8, tzinfo=timezone.utc)
    cursor.fetchone.side_effect = [(ATTEMPT_ID, "uncertain", None)]
    cursor.fetchall.return_value = [
        (None, "pending", "claim", "workflow", None, None, recorded_at),
        ("pending", "uncertain", "timeout", "system", "No outcome", None, recorded_at),
    ]
    response = client.get(f"/bookings/{REF}/confirmation-attempt", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["state"] == "uncertain"
    assert [event["action"] for event in response.json()["history"]] == ["claim", "timeout"]
    assert "ORDER BY id" in cursor.execute.call_args.args[0]


def test_identical_outcome_is_idempotent_without_a_second_audit_event(api):
    client, _, cursor = api
    cursor.fetchone.return_value = ("confirmed", "synthetic-gmail-id")
    response = client.post(f"{ATTEMPT_URL}/outcome", headers=HEADERS,
                           json={"state": "confirmed", "gmail_message_id": "synthetic-gmail-id"})
    assert response.status_code == 200
    assert cursor.execute.call_count == 1


def test_concurrent_claims_create_one_attempt(monkeypatch):
    """Model the transaction lock with shared state; both callers race to claim."""
    state = {"claimed_at": None, "attempt": None, "inserts": 0, "events": 0}
    transaction_lock = Lock()
    start = Barrier(2)

    class Cursor:
        def __init__(self):
            self.row = None

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, sql, params=None):
            if "SELECT id, status, confirmation_claimed_at" in sql:
                self.row = (7, "confirmed", state["claimed_at"])
            elif "SELECT id, state, gmail_message_id FROM booking_confirmation_attempts" in sql:
                self.row = state["attempt"]
            elif "UPDATE bookings SET confirmation_claimed_at" in sql:
                state["claimed_at"] = datetime.now(timezone.utc)
            elif "INSERT INTO booking_confirmation_attempts" in sql:
                state["attempt"] = (params[0], params[2], None)
                state["inserts"] += 1
            elif "INSERT INTO booking_confirmation_events" in sql:
                state["events"] += 1
            elif "UPDATE booking_confirmation_attempts" in sql:
                self.row = None
            else:
                raise AssertionError(sql)

        def fetchone(self):
            return self.row

    class Connection:
        def __enter__(self):
            transaction_lock.acquire()
            return self

        def __exit__(self, *_):
            transaction_lock.release()
            return False

        def cursor(self):
            return Cursor()

    monkeypatch.setattr(main.psycopg, "connect", lambda *_: Connection())
    def claim():
        start.wait()
        result = main.claim_booking_confirmation(REF)
        return result.status_code if hasattr(result, "status_code") else 201

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        assert sorted(f.result() for f in futures) == [200, 201]
    assert state["inserts"] == state["events"] == 1
