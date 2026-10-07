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
