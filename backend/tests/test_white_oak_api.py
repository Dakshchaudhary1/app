"""Regression coverage for White Oak public booking, notifications, AI, and auth gates."""
import os
from datetime import date, timedelta

import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/frontend/.env")
BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")


@pytest.fixture(scope="module")
def api():
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    return session


@pytest.fixture(scope="module")
def booking_date():
    return (date.today() + timedelta(days=45)).isoformat()


def test_menu_and_tables_are_seeded(api):
    menu = api.get(f"{BASE_URL}/api/menu", timeout=20)
    tables = api.get(f"{BASE_URL}/api/tables", timeout=20)
    assert menu.status_code == 200 and len(menu.json()) >= 10
    assert tables.status_code == 200 and len(tables.json()) == 12
    assert all("_id" not in row for row in menu.json() + tables.json())


def test_future_availability_has_real_slots(api, booking_date):
    response = api.post(f"{BASE_URL}/api/availability", json={"date": booking_date, "guests": 2}, timeout=20)
    assert response.status_code == 200
    body = response.json()
    assert body["date"] == booking_date and body["slots"]
    assert {"time", "table_id", "capacity"}.issubset(body["slots"][0])


def test_booking_lookup_and_cancellation(api, booking_date):
    payload = {"date": booking_date, "start_time": "19:00", "number_of_guests": 2,
               "customer_name": "TEST API Guest", "customer_whatsapp": "+919876543210"}
    created = api.post(f"{BASE_URL}/api/reservations", json=payload, timeout=20)
    assert created.status_code == 200, created.text
    reservation = created.json()["reservation"]
    assert reservation["status"] == "confirmed" and reservation["confirmation_code"].startswith("WO-")
    assert created.json()["whatsapp"]["status"] == "queued_mock"
    lookup = api.post(f"{BASE_URL}/api/reservations/lookup", json={"booking_code": reservation["confirmation_code"]}, timeout=20)
    assert lookup.status_code == 200 and lookup.json()["reservation_id"] == reservation["reservation_id"]
    cancelled = api.delete(f"{BASE_URL}/api/reservations/{reservation['reservation_id']}", timeout=20)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert api.post(f"{BASE_URL}/api/reservations/lookup", json={"booking_code": reservation["confirmation_code"]}, timeout=20).json()["status"] == "cancelled"


def test_whatsapp_is_explicitly_mocked(api):
    response = api.post(f"{BASE_URL}/api/notifications/whatsapp", timeout=20)
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False and body["status"] == "mocked"


def test_ai_returns_reply_or_truthful_fallback(api):
    response = api.post(f"{BASE_URL}/api/ai/chat", json={"message": "What time do you open?", "history": []}, timeout=40)
    assert response.status_code == 200 and isinstance(response.json().get("reply"), str)
    assert response.json()["reply"]


def test_admin_summary_requires_auth(api):
    response = api.get(f"{BASE_URL}/api/admin/summary", timeout=20)
    assert response.status_code == 401


def test_conflicting_explicit_table_is_rejected(api, booking_date):
    payload = {"date": booking_date, "start_time": "20:00", "number_of_guests": 2,
               "customer_name": "TEST Conflict One", "customer_whatsapp": "+919876543211"}
    first = api.post(f"{BASE_URL}/api/reservations", json=payload, timeout=20)
    assert first.status_code == 200, first.text
    table_id = first.json()["reservation"]["table_id"]
    payload.update({"customer_name": "TEST Conflict Two", "customer_whatsapp": "+919876543212", "table_id": table_id})
    second = api.post(f"{BASE_URL}/api/reservations", json=payload, timeout=20)
    assert second.status_code == 409
    api.delete(f"{BASE_URL}/api/reservations/{first.json()['reservation']['reservation_id']}", timeout=20)


def test_reservation_update_rejects_an_overlap(api, booking_date):
    """Modification must not move a booking onto an already occupied table/time."""
    first_payload = {"date": booking_date, "start_time": "21:00", "number_of_guests": 2,
                     "customer_name": "TEST Update Occupied", "customer_whatsapp": "+919876543213"}
    first = api.post(f"{BASE_URL}/api/reservations", json=first_payload, timeout=20)
    assert first.status_code == 200, first.text
    other = dict(first_payload, start_time="22:00", customer_name="TEST Update Target", customer_whatsapp="+919876543214")
    second = api.post(f"{BASE_URL}/api/reservations", json=other, timeout=20)
    assert second.status_code == 200, second.text
    reservation_id = second.json()["reservation"]["reservation_id"]
    moved = api.patch(f"{BASE_URL}/api/reservations/{reservation_id}", json={"start_time": "21:00"}, timeout=20)
    assert moved.status_code == 409