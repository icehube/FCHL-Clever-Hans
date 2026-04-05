"""HTMX interaction smoke tests.

Verifies HTMX-specific behaviors that pytest can check without a browser:
toast headers, OOB swap IDs, data attributes, form element IDs, and
validation responses.
"""

import json
import re

import pytest
from fastapi.testclient import TestClient

from config import MAX_SALARY, MIN_SALARY


@pytest.fixture(scope="module")
def client():
    from main import app
    with TestClient(app) as c:
        c.post("/reset")
        yield c


class TestToastHeaders:
    """Every mutation endpoint should return HX-Trigger with showToast."""

    def test_assign_success_toast(self, client):
        """Successful assign returns success toast."""
        r = client.post("/assign", data={
            "player": "Artemi Panarin", "team": "BOT", "salary": "5.0",
        })
        trigger = json.loads(r.headers.get("HX-Trigger", "{}"))
        assert "showToast" in trigger
        assert trigger["showToast"]["type"] == "success"
        assert "Panarin" in trigger["showToast"]["message"]
        client.post("/undo")

    def test_assign_invalid_team_toast(self, client):
        """Assign with invalid team returns error toast, not 500."""
        r = client.post("/assign", data={
            "player": "Artemi Panarin", "team": "FAKE", "salary": "5.0",
        })
        assert r.status_code == 200  # Not 500
        trigger = json.loads(r.headers.get("HX-Trigger", "{}"))
        assert "showToast" in trigger
        assert trigger["showToast"]["type"] == "error"

    def test_assign_missing_player_toast(self, client):
        """Assign with nonexistent player returns warning toast."""
        r = client.post("/assign", data={
            "player": "Nobody", "team": "BOT", "salary": "1.0",
        })
        assert r.status_code == 200
        trigger = json.loads(r.headers.get("HX-Trigger", "{}"))
        assert "showToast" in trigger
        assert trigger["showToast"]["type"] == "warning"
        client.post("/undo")

    def test_buyout_success_toast(self, client):
        """Successful buyout returns success toast."""
        r = client.post("/buyout", data={"player": "Dougie Hamilton"})
        trigger = json.loads(r.headers.get("HX-Trigger", "{}"))
        assert "showToast" in trigger
        assert trigger["showToast"]["type"] == "success"
        client.post("/undo")

    def test_buyout_failure_toast(self, client):
        """Failed buyout returns error toast."""
        r = client.post("/buyout", data={"player": "Nobody"})
        trigger = json.loads(r.headers.get("HX-Trigger", "{}"))
        assert "showToast" in trigger
        assert trigger["showToast"]["type"] == "error"


class TestAssignValidation:
    """Assign endpoint validates and clamps inputs."""

    def test_salary_clamped_to_min(self, client):
        """Salary below MIN_SALARY should be clamped up."""
        r = client.post("/assign", data={
            "player": "Artemi Panarin", "team": "BOT", "salary": "0.1",
        })
        assert r.status_code == 200
        trigger = json.loads(r.headers.get("HX-Trigger", "{}"))
        assert f"${MIN_SALARY}M" in trigger["showToast"]["message"]
        client.post("/undo")

    def test_salary_clamped_to_max(self, client):
        """Salary above MAX_SALARY should be clamped down."""
        r = client.post("/assign", data={
            "player": "Filip Forsberg", "team": "BOT", "salary": "50.0",
        })
        assert r.status_code == 200
        trigger = json.loads(r.headers.get("HX-Trigger", "{}"))
        assert f"${MAX_SALARY}M" in trigger["showToast"]["message"]
        client.post("/undo")


class TestOOBSwapIDs:
    """Buyout indicator OOB swap IDs must match roster panel placeholders."""

    def test_ids_match_after_reset(self, client):
        """After fresh reset, all OOB IDs should match placeholders."""
        client.post("/reset")
        idx = client.get("/")
        main_ids = set(re.findall(r'id="bo-([^"]+)"', idx.text))

        r = client.get("/buyout-indicators")
        dot_ids = set(re.findall(r'id="bo-([^"]+)"', r.text))

        assert main_ids == dot_ids, f"Mismatch: {main_ids ^ dot_ids}"

    def test_ids_match_after_assign(self, client):
        """After assigning a player, OOB IDs still match."""
        client.post("/assign", data={
            "player": "Artemi Panarin", "team": "BOT", "salary": "5.0",
        })
        idx = client.get("/")
        main_ids = set(re.findall(r'id="bo-([^"]+)"', idx.text))

        r = client.get("/buyout-indicators")
        dot_ids = set(re.findall(r'id="bo-([^"]+)"', r.text))

        assert main_ids == dot_ids
        client.post("/undo")

    def test_no_invalid_html_chars_in_ids(self, client):
        """All bo- IDs should contain only valid HTML ID characters."""
        r = client.get("/buyout-indicators")
        ids = re.findall(r'id="bo-([^"]+)"', r.text)
        for bid in ids:
            assert re.match(r'^[A-Za-z0-9_-]+$', bid), f"Invalid ID chars: bo-{bid}"


class TestPositionFilterAttributes:
    """Available players table rows must have data-position for JS filtering."""

    def test_all_rows_have_data_position(self, client):
        """Every available player row should have data-position."""
        r = client.get("/")
        # Count rows with data-position vs total rows in bid-limits tbody
        positions = re.findall(r'data-position="([^"]+)"', r.text)
        assert len(positions) > 100, f"Expected 100+ rows with data-position, got {len(positions)}"

    def test_positions_are_valid(self, client):
        """All data-position values should be F, D, or G."""
        r = client.get("/")
        positions = set(re.findall(r'data-position="([^"]+)"', r.text))
        assert positions <= {"F", "D", "G"}, f"Invalid positions: {positions - {'F', 'D', 'G'}}"


class TestBidPriceInput:
    """Bid form must have id='bid-price' for the adjustPrice() JS function."""

    def test_inactive_form_has_bid_price_id(self, client):
        """The initial bid form (no active bid) should have id='bid-price'."""
        client.post("/reset")
        r = client.get("/")
        assert 'id="bid-price"' in r.text, "Inactive bid form missing id='bid-price'"

    def test_active_form_has_bid_price_id(self, client):
        """After bid-check, the active form should have id='bid-price'."""
        r = client.post("/bid-check", data={
            "player": "Artemi Panarin", "price": "2.0", "bidders": "",
        })
        assert 'id="bid-price"' in r.text, "Active bid form missing id='bid-price'"


class TestResetIdempotency:
    """Two consecutive resets should produce identical state."""

    def test_double_reset(self, client):
        """Reset twice — state should be identical."""
        client.post("/reset")
        r1 = client.get("/state")
        state1 = r1.json()

        client.post("/reset")
        r2 = client.get("/state")
        state2 = r2.json()

        assert len(state1["available_players"]) == len(state2["available_players"])
        assert len(state1["transaction_log"]) == len(state2["transaction_log"]) == 0
        assert set(state1["teams"].keys()) == set(state2["teams"].keys())
