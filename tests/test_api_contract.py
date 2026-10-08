"""Checks for API behavior that must not depend on a running database."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import main


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "test-api-key")
    return TestClient(main.app)


def test_health_check(client, monkeypatch):
    monkeypatch.setattr(main, "API_KEY", None)

    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {"message": "nova-api is alive"}


@pytest.mark.parametrize("headers", [{}, {"x-api-key": "wrong"}])
@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("get", "/contacts", {}),
        ("get", "/customers/1/orders", {}),
        ("get", "/portal/summary", {}),
        ("get", "/inquiries/dedupe?sender=lead@example.com", {}),
        (
            "post",
            "/inquiries",
            {"json": {
                "sender_email": "lead@example.com",
                "inquiry_text": "I am interested in this property",
                "no_send_reason": "Human review required",
            }},
        ),
        ("get", "/bookings/lookup?email=client@example.com", {}),
        (
            "post",
            "/bookings",
            {"json": {
                "client_email": "client@example.com",
                "treatment": "Massage",
                "starts_at": "2026-10-08T10:00:00+07:00",
                "ends_at": "2026-10-08T11:00:00+07:00",
                "calendar_event_id": "event-1",
            }},
        ),
        (
            "patch",
            "/bookings/CMW-1234-abcd/cancel",
            {"json": {"client_email": "client@example.com"}},
        ),
        (
            "post",
            "/bookings/CMW-1234-abcd/reschedule",
            {"json": {
                "client_email": "client@example.com",
                "starts_at": "2026-10-09T10:00:00+07:00",
                "ends_at": "2026-10-09T11:00:00+07:00",
                "calendar_event_id": "event-2",
            }},
        ),
    ],
)
def test_protected_endpoints_reject_missing_or_wrong_key(
    client, monkeypatch, headers, method, path, kwargs
):
    def unexpected_connection(*args, **kwargs):
        pytest.fail("Authentication must fail before opening a database connection")

    monkeypatch.setattr(main.psycopg, "connect", unexpected_connection)

    response = getattr(client, method)(path, headers=headers, **kwargs)

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API key"}


@pytest.mark.parametrize(
    "path",
    [
        "/contacts",
        "/customers/1/orders",
        "/portal/summary",
        "/inquiries/dedupe?sender=lead@example.com",
    ],
)
def test_missing_server_key_fails_closed(client, monkeypatch, path):
    monkeypatch.setattr(main, "API_KEY", None)

    def unexpected_connection(*args, **kwargs):
        pytest.fail("Authentication must fail before opening a database connection")

    monkeypatch.setattr(main.psycopg, "connect", unexpected_connection)

    response = client.get(
        path,
        headers={"x-api-key": "test-api-key"},
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/contacts", {"contacts": []}),
        ("/customers/1/orders", {"customer_id": 1, "orders": []}),
        ("/portal/summary", {"summary": []}),
    ],
)
def test_valid_key_allows_crm_reads(client, monkeypatch, path, expected):
    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (1,)
    cursor.fetchall.return_value = []
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(main.psycopg, "connect", connect)

    response = client.get(path, headers={"x-api-key": "test-api-key"})

    assert response.status_code == 200
    assert response.json() == expected
    connect.assert_called_once_with(main.DB)


def test_valid_key_allows_dedupe_request(client, monkeypatch):
    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (None,)
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(main.psycopg, "connect", connect)

    response = client.get(
        "/inquiries/dedupe?sender=lead@example.com",
        headers={"x-api-key": "test-api-key"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "already_replied_today": False,
        "last_reply_at": None,
    }
    connect.assert_called_once_with(main.DB)
    assert cursor.execute.call_args.args[1] == ("lead@example.com",)


@pytest.mark.parametrize(
    "reply_text,no_send_reason,valid",
    [
        ("Reply sent", None, True),
        (None, "Human review required", True),
        (None, None, False),
        ("Reply sent", "Human review required", False),
    ],
)
def test_inquiry_requires_exactly_one_reply_outcome(reply_text, no_send_reason, valid):
    payload = {
        "sender_email": "lead@example.com",
        "inquiry_text": "I am interested in this property",
        "reply_text": reply_text,
        "no_send_reason": no_send_reason,
    }

    if valid:
        inquiry = main.InquiryIn.model_validate(payload)
        assert inquiry.reply_text == reply_text
        assert inquiry.no_send_reason == no_send_reason
    else:
        with pytest.raises(ValidationError, match="exactly one"):
            main.InquiryIn.model_validate(payload)


@pytest.fixture
def booking_db(monkeypatch):
    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    monkeypatch.setattr(main.psycopg, "connect", MagicMock(return_value=connection))
    return connection, cursor


@pytest.fixture
def booking_payload():
    return {
        "client_email": "client@example.com",
        "client_name": "Client",
        "treatment": "Massage",
        "starts_at": "2026-10-08T10:00:00+07:00",
        "ends_at": "2026-10-08T11:00:00+07:00",
        "calendar_event_id": "event-1",
        "thread_id": "thread-1",
    }


@pytest.fixture
def reschedule_payload():
    return {
        "client_email": "client@example.com",
        "starts_at": "2026-10-09T10:00:00+07:00",
        "ends_at": "2026-10-09T11:00:00+07:00",
        "calendar_event_id": "event-2",
    }


def test_create_booking_returns_original_for_matching_event_replay(
    client, booking_db, booking_payload
):
    _, cursor = booking_db
    cursor.fetchone.side_effect = [(12, "CMW-original"), None, (12, "CMW-original")]

    first = client.post("/bookings", json=booking_payload,
                        headers={"x-api-key": "test-api-key"})
    repeat = client.post("/bookings", json=booking_payload,
                         headers={"x-api-key": "test-api-key"})

    assert first.status_code == repeat.status_code == 201
    assert first.json() == repeat.json() == {"id": 12, "booking_ref": "CMW-original"}
    assert cursor.execute.call_count == 3
    assert "ON CONFLICT DO NOTHING" in cursor.execute.call_args_list[0].args[0]
    assert "calendar_event_id = %s" in cursor.execute.call_args_list[2].args[0]


def test_create_booking_rejects_event_reuse_or_overlapping_client_booking(
    client, booking_db, booking_payload
):
    connection, cursor = booking_db
    cursor.fetchone.side_effect = [None, None]

    response = client.post("/bookings", json=booking_payload,
                           headers={"x-api-key": "test-api-key"})

    assert response.status_code == 409
    assert cursor.execute.call_count == 2
    assert connection.__exit__.call_args.args[0] is main.HTTPException


def test_reschedule_claims_old_booking_before_inserting_successor(
    client, booking_db, reschedule_payload
):
    _, cursor = booking_db
    old = (1, "CMW-old", "client@example.com", "Massage", None, None,
           "event-1", "confirmed")
    cursor.fetchone.side_effect = [None, old, (1,), ("CMW-new",)]

    response = client.post("/bookings/CMW-old/reschedule", json=reschedule_payload,
                           headers={"x-api-key": "test-api-key"})

    assert response.status_code == 200
    assert response.json() == {
        "booking_ref": "CMW-new", "previous_ref": "CMW-old",
        "old_calendar_event_id": "event-1",
    }
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert "WHERE id = %s AND status = 'confirmed' RETURNING id" in statements[2]
    assert "ON CONFLICT DO NOTHING RETURNING booking_ref" in statements[3]


def test_reschedule_returns_original_for_matching_event_replay(
    client, booking_db, reschedule_payload
):
    _, cursor = booking_db
    cursor.fetchone.return_value = ("CMW-new", "CMW-old", "event-1", True)

    response = client.post("/bookings/CMW-old/reschedule", json=reschedule_payload,
                           headers={"x-api-key": "test-api-key"})

    assert response.status_code == 200
    assert response.json() == {
        "booking_ref": "CMW-new", "previous_ref": "CMW-old",
        "old_calendar_event_id": "event-1",
    }
    assert cursor.execute.call_count == 1


def test_reschedule_rejects_event_reuse_with_different_request(
    client, booking_db, reschedule_payload
):
    _, cursor = booking_db
    cursor.fetchone.return_value = ("CMW-new", "CMW-old", "event-1", False)

    response = client.post("/bookings/CMW-old/reschedule", json=reschedule_payload,
                           headers={"x-api-key": "test-api-key"})

    assert response.status_code == 409
    assert cursor.execute.call_count == 1


@pytest.mark.parametrize(
    "raced_replay,expected_status",
    [(("CMW-new", "CMW-old", "event-1", True), 200), (None, 409)],
)
def test_reschedule_handles_lost_claim_after_concurrent_request(
    client, booking_db, reschedule_payload, raced_replay, expected_status
):
    connection, cursor = booking_db
    old = (1, "CMW-old", "client@example.com", "Massage", None, None,
           "event-1", "confirmed")
    cursor.fetchone.side_effect = [None, old, None, raced_replay]

    response = client.post("/bookings/CMW-old/reschedule", json=reschedule_payload,
                           headers={"x-api-key": "test-api-key"})

    assert response.status_code == expected_status
    assert cursor.execute.call_count == 4
    assert all("INSERT INTO bookings" not in call.args[0]
               for call in cursor.execute.call_args_list)
    if expected_status == 409:
        assert connection.__exit__.call_args.args[0] is main.HTTPException
    else:
        assert response.json()["booking_ref"] == "CMW-new"


def test_reschedule_rolls_back_claim_when_new_booking_conflicts(
    client, booking_db, reschedule_payload
):
    connection, cursor = booking_db
    old = (1, "CMW-old", "client@example.com", "Massage", None, None,
           "event-1", "confirmed")
    cursor.fetchone.side_effect = [None, old, (1,), None]

    response = client.post("/bookings/CMW-old/reschedule", json=reschedule_payload,
                           headers={"x-api-key": "test-api-key"})

    assert response.status_code == 409
    assert connection.__exit__.call_args.args[0] is main.HTTPException
