"""HTMX interaction smoke tests.

Verifies HTMX-specific behaviors that pytest can check without a browser:
toast headers, OOB swap IDs, data attributes, form element IDs, and
validation responses.
"""

import json
import os
import re

import pytest
from fastapi.testclient import TestClient

from config import MAX_SALARY, MIN_SALARY

# Repo-relative so the scan works from any rootdir pytest is invoked with.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(_REPO_ROOT, "templates")
SHORTCUTS_JS = os.path.join(_REPO_ROOT, "static", "shortcuts.js")


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

    def test_salary_quantized_to_the_auction_increment(self, client):
        """A sub-increment salary must never reach the roster.

        The price box can't stop it: step= only drives the spinner, and the box
        lives in a different form from Assign so its validity is never checked
        on submit. Recording 2.55 puts a price in the draft record that the CBA
        has no increment for, and lands total_salary on a half-step — stranding
        $0.05M of cap below the increment remaining_budget floors to.
        """
        import main

        for raw, expected in [("2.54", 2.5), ("3.06", 3.1), ("1.96", 2.0)]:
            r = client.post("/assign", data={
                "player": "Artemi Panarin", "team": "BOT", "salary": raw,
            })
            assert r.status_code == 200
            p = main.auction_state.teams["BOT"].find_player("Artemi Panarin")
            assert p.salary == expected, f"${raw}M recorded as ${p.salary}M"
            client.post("/undo")

    def test_exact_half_increment_still_lands_on_a_step(self, client):
        """2.55 is a tie; which way it breaks depends on the float, not on
        behaviour worth pinning. What matters is that it lands on a step."""
        import main

        r = client.post("/assign", data={
            "player": "Artemi Panarin", "team": "BOT", "salary": "2.55",
        })
        assert r.status_code == 200
        p = main.auction_state.teams["BOT"].find_player("Artemi Panarin")
        tenths = p.salary * 10
        assert abs(tenths - round(tenths)) < 1e-9, f"${p.salary}M is off-step"
        trigger = json.loads(r.headers.get("HX-Trigger", "{}"))
        assert "adjusted from $2.55M" in trigger["showToast"]["message"]
        client.post("/undo")

    def test_adjust_salary_quantizes_too(self, client):
        """The typo-fix endpoint had the same hole, and it sees the most
        fat-fingered input of any — storing something other than what was
        typed is the failure it exists to correct. _log_change formats to .1f,
        so the change log read "$2.5M" while the roster held 2.55.
        """
        import main

        client.post("/assign", data={
            "player": "Artemi Panarin", "team": "BOT", "salary": "2.5",
        })
        r = client.post("/adjust-salary", data={
            "team_code": "BOT", "player_name": "Artemi Panarin",
            "new_salary": "2.54",
        })
        assert r.status_code == 200
        t = main.auction_state.teams["BOT"]
        assert t.find_player("Artemi Panarin").salary == 2.5
        tenths = t.total_salary * 10
        assert abs(tenths - round(tenths)) < 1e-9, f"${t.total_salary}M is off-step"
        trigger = json.loads(r.headers.get("HX-Trigger", "{}"))
        assert "adjusted from $2.54M" in trigger["showToast"]["message"]
        client.post("/undo")
        client.post("/undo")

    def test_adjust_salary_stays_quiet_on_a_legal_price(self, client):
        """No toast when the typed value is recorded verbatim."""
        client.post("/assign", data={
            "player": "Artemi Panarin", "team": "BOT", "salary": "2.5",
        })
        r = client.post("/adjust-salary", data={
            "team_code": "BOT", "player_name": "Artemi Panarin",
            "new_salary": "3.1",
        })
        assert r.status_code == 200
        assert "showToast" not in r.headers.get("HX-Trigger", "")
        client.post("/undo")
        client.post("/undo")

    def test_a_legal_price_is_not_reported_as_adjusted(self, client):
        """The note must fire only on a real change — a spurious 'adjusted'
        on every pick would train the operator to ignore it."""
        r = client.post("/assign", data={
            "player": "Artemi Panarin", "team": "BOT", "salary": "2.5",
        })
        assert r.status_code == 200
        trigger = json.loads(r.headers.get("HX-Trigger", "{}"))
        assert "adjusted" not in trigger["showToast"]["message"]
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


class TestSwapTargetsResolve:
    """Every hx-target must name an element that actually exists.

    htmx fails a bad target *silently* — it logs to the console and swaps
    nothing, so the app just looks dead. Nothing else catches this: the
    endpoint tests assert what a response contains, never that the thing it
    is aimed at is there. Added after the 2026-08-06 panel split re-pointed
    seven triggers and one htmx.ajax call in shortcuts.js; a typo like
    `#nomination_panel` would have passed the entire suite and only shown up
    mid-draft as a key that did nothing.

    Targets are collected from the templates and from shortcuts.js, which is
    the only place a swap target lives outside an attribute.
    """

    def _targets(self) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for root, _, files in os.walk(TEMPLATE_DIR):
            for name in files:
                path = os.path.join(root, name)
                with open(path) as fh:
                    for m in re.finditer(r'hx-target="#([\w-]+)"', fh.read()):
                        found.setdefault(m.group(1), []).append(path)
        with open(SHORTCUTS_JS) as fh:
            for m in re.finditer(r"target:\s*'#([\w-]+)'", fh.read()):
                found.setdefault(m.group(1), []).append(SHORTCUTS_JS)
        return found

    def _rendered_states(self, client) -> str:
        """Every DOM the auction panel can be in, concatenated.

        A single `GET /` is not enough: some regions are conditional, and a
        target inside one is unreachable until that branch renders. #bid-advice
        is the case — it exists only while a bid is live, and so does the price
        input that targets it, so the pair is always consistent even though
        neither is on a fresh page. Checking the union keeps the guard honest
        about "does this id ever exist" without failing on that pairing.
        """
        page = client.get("/").text
        active_bid = client.post("/bid-check", data={
            "player": "Connor McDavid", "price": "3.0", "bidders": "BOT,LGN,SRL",
        }).text
        return page + active_bid

    def test_every_target_exists_in_the_rendered_page(self, client):
        dom = self._rendered_states(client)
        broken = {
            target: sources
            for target, sources in self._targets().items()
            if f'id="{target}"' not in dom
        }
        assert not broken, (
            "hx-target names an element that does not exist in any rendered "
            "state — htmx will swap nothing and log only to the console: "
            f"{broken}"
        )

    def test_no_id_appears_twice_in_the_page(self, client):
        """Existing is not enough — a target must resolve to ONE element.

        htmx and getElementById both take the first match and neither
        complains, so a duplicated id sends a swap to whichever copy happens to
        come first in document order.

        Scoped to `GET /` rather than the union of rendered states: fragments
        legitimately repeat the ids of the regions they replace, so
        concatenating responses would report every swap target as a duplicate.

        **This would NOT have caught the player-chart duplicate**, and the
        limit is worth stating rather than discovering later: that duplicate
        only existed in the ASSEMBLED DOM — the chart body is absent from
        `GET /` and arrives via a swap — so no single response contained both
        copies. What covers that is `TestPlayerChart` on the structural
        property (the body owns no mount id) plus the browser test that clicks
        a chart link with a bid live. This guard covers the simpler case: one
        template rendering the same id twice on one page.
        """
        page = client.get("/").text
        ids = re.findall(r'\sid="([\w-]+)"', page)
        dupes = {i: ids.count(i) for i in set(ids) if ids.count(i) > 1}
        assert not dupes, (
            "duplicate id in one document — htmx resolves a target to the "
            f"first match and swaps into the wrong element: {dupes}"
        )

    def test_the_split_panels_are_both_targeted_and_present(self, client):
        """The two ids the panel split introduced, specifically."""
        targets = self._targets()
        page = client.get("/").text
        for panel in ("nomination-panel", "bid-panel"):
            assert panel in targets, f"#{panel} is no longer targeted by anything"
            assert f'id="{panel}"' in page, f"#{panel} missing from the page"

    def test_the_n_shortcut_targets_the_nomination_panel(self, client):
        """The bare `n` key must not be able to reach the bid session.

        This is the regression the split fixed: shortcuts.js fires /nominate
        on a keypress whose guard only covers INPUT/TEXTAREA/SELECT, so focus
        on a button leaves it live. Pointing it back at #auction-control would
        restore the bug with every response-level test still green.
        """
        with open(SHORTCUTS_JS) as fh:
            js = fh.read()
        nominate = re.search(r"htmx\.ajax\('GET',\s*'/nominate',\s*\{([^}]*)\}", js)
        assert nominate, "the /nominate shortcut is gone or was reshaped"
        assert "'#nomination-panel'" in nominate.group(1), (
            f"`n` must target #nomination-panel, got: {nominate.group(1)}"
        )


class TestCounterfactualAutoLoads:
    """The counterfactual arrives by itself, and lands where it can't do harm.

    Both placement rules below are invisible at the call site — the mount looks
    like a div that could sit anywhere in the panel — and a tidy-up that moved
    it next to the verdict, where it visually belongs, would silently reopen a
    bug. So they are asserted on position, with the reason in the message.
    """

    def _live_panel(self, client) -> str:
        """A bid panel with a single bidder, so the Assign form renders too."""
        r = client.post("/bid-check", data={
            "player": "Connor McDavid", "price": "3.0", "bidders": "BOT",
        })
        assert r.status_code == 200
        assert 'hx-post="/assign"' in r.text, "fixture needs the Assign form"
        return r.text

    def test_the_mount_requests_the_current_player(self, client):
        panel = self._live_panel(client)
        mount = re.search(
            r'<div id="bid-counterfactual".*?hx-get="([^"]+)".*?hx-trigger="([^"]+)"',
            panel, re.S,
        )
        assert mount, "the bid panel no longer auto-loads a counterfactual"
        assert "Connor%20McDavid" in mount.group(1), mount.group(1)
        assert "inline=1" in mount.group(1), (
            "must request the body-only fragment; the full panel would put a "
            "second id=\"explanation\" on the page"
        )
        assert mount.group(2) == "load"

    def test_the_mount_is_outside_the_price_swap_region(self, client):
        """#bid-advice is what a price change replaces (hx-select).

        Inside it, `load` would re-fire on every keystroke in the price box.
        """
        panel = self._live_panel(client)
        advice_open = panel.index('id="bid-advice"')
        advice_close = panel.index('id="bid-form"')  # first element after it
        mount = panel.index('id="bid-counterfactual"')
        assert not advice_open < mount < advice_close, (
            "the counterfactual mount is inside #bid-advice, so every price "
            "change would re-trigger its load"
        )

    def test_the_mount_is_after_the_assign_button(self, client):
        """A fragment arriving ~200ms late must not reflow the controls.

        Above the Assign form, the analysis inserting itself would push the
        button down under a pointer already travelling towards it — the same
        class of bug as the blur race that swallowed Assign clicks.
        """
        panel = self._live_panel(client)
        assert panel.index('id="bid-counterfactual"') > panel.rindex('hx-post="/assign"'), (
            "the counterfactual mount sits above the Assign form; a late "
            "response would move the button mid-click"
        )
        assert panel.index('id="bid-counterfactual"') > panel.index('id="bid-price"'), (
            "the counterfactual mount sits above the price input"
        )


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


class TestShortcutsModal:
    """The shortcuts list must describe the handler, not a memory of it.

    A stale list is worse than none: it is read once, believed, and then acted
    on mid-draft. So the modal's rows carry `data-shortcut-key` and this class
    compares that set against the keys `shortcuts.js` actually binds, in both
    directions — adding a shortcut without documenting it fails just as loudly
    as documenting one that does not exist.
    """

    def _documented(self) -> set[str]:
        with open(os.path.join(TEMPLATE_DIR, "base.html")) as fh:
            return set(re.findall(r'data-shortcut-key="([^"]+)"', fh.read()))

    def _bound(self) -> set[str]:
        with open(SHORTCUTS_JS) as fh:
            return set(re.findall(r"e\.key\.toLowerCase\(\) === '([a-z])'", fh.read()))

    def test_the_button_and_the_dialog_render(self, client):
        page = client.get("/").text
        assert 'id="shortcuts-modal"' in page
        assert "showModal()" in page, "nothing opens the dialog"

    def test_every_bound_key_is_documented(self):
        bound, documented = self._bound(), self._documented()
        assert bound, "no key bindings found — the regex stopped matching"
        assert bound <= documented, (
            f"shortcuts.js binds {sorted(bound - documented)} but the modal "
            f"does not list them — the list is now a lie by omission"
        )

    def test_every_documented_key_is_bound(self):
        bound, documented = self._bound(), self._documented()
        assert documented <= bound, (
            f"the modal advertises {sorted(documented - bound)}, which "
            f"shortcuts.js does not handle — pressing it does nothing"
        )

    def test_the_dialog_survives_a_panel_swap(self, client):
        """Mounted outside #app, like the startup banner.

        Every mutating endpoint returns all_panels.html into #app, so a dialog
        inside it would be destroyed mid-read the moment a pick landed.
        """
        page = client.get("/").text
        assert page.index('id="shortcuts-modal"') < page.index('id="app"')
