"""Lineup-slot numbering in the team panel's roster table.

The panel lists the roster grouped F -> D -> G with points descending, and
until 2026-09-20 nothing counted it: answering "how many forwards do I still
need?" meant counting rows by eye, mid-bid, in a table that scrolls
horizontally. `Needs: 3F · 1D` (from `roster_needs`) gives the aggregate and
never the running count beside the player.

`main._roster_slots` labels each row `F1`..`F12`, `D1`..`D6`, `G1`..`G2` for
players who will start, `BF1`..`BG4` for benched ones, and nothing at all for a
MILP suggested buy. The last starter number in a group is how many of that
position start; the holding adds the group's bench labels (`F1`..`F5` plus a
`BF1` is six forwards), and the two agree until somebody is benched.

The unit tests below drive the function directly because it is a pure pass over
already-sorted rows; the endpoint tests check the column is actually wired to
it, which is the half a unit test cannot see.
"""

import re

import pytest

import main
from config import MY_TEAM
from tests.helpers import a_roster_player, an_eligible_minor, assign, pool_top, section_of


# --------------------------------------------------------------------------
# The rule itself
# --------------------------------------------------------------------------


def _row(position, *, bench=False, target=False):
    """One roster row in the shape `team_panel.html` builds."""
    return {"position": position, "is_bench": bench, "is_target": target}


class TestTheSlotRule:
    """`_roster_slots` over rows already in display order."""

    def test_each_position_counts_independently(self):
        rows = main._roster_slots(
            [_row("F"), _row("F"), _row("F"), _row("D"), _row("D"), _row("G")]
        )
        assert [r["slot"] for r in rows] == ["F1", "F2", "F3", "D1", "D2", "G1"]

    def test_a_suggested_buy_has_no_slot_and_consumes_no_number(self):
        """The whole point of the empty label.

        A target is not owned — the MILP suggests it, nobody bought it — so
        letting it take `F2` would report a roster the team does not have.
        Asserting the label is blank is the weaker half; what matters is that
        the forward AFTER it is still `F2`, i.e. the counter never moved.
        """
        rows = main._roster_slots([_row("F"), _row("F", target=True), _row("F")])
        assert [r["slot"] for r in rows] == ["F1", "", "F2"]

    def test_the_bench_number_is_one_sequence_across_every_position(self):
        """The NUMBER runs 1..4 over the whole bench; the LETTER is the position.

        Not a per-position sequence: the second bench player is `BD2` even
        though he is the first benched defenceman. The number answers "how full
        is the bench" against the hard `BENCH_SIZE` of 4, which is what binds —
        bench composition against `BACKUP_TARGETS` is a soft MILP preference and
        would be the wrong thing to count.
        """
        rows = main._roster_slots(
            [_row("F"), _row("F", bench=True), _row("D"), _row("D", bench=True)]
        )
        assert [r["slot"] for r in rows] == ["F1", "BF1", "D1", "BD2"]

    def test_a_bench_label_names_its_own_position(self):
        """Every row carries its position — the reason the Pos column could go.

        `B1` alone left up to four rows whose position nothing on the panel
        stated once `Pos` was dropped.
        """
        rows = main._roster_slots(
            [_row("F", bench=True), _row("D", bench=True), _row("G", bench=True)]
        )
        assert [r["slot"] for r in rows] == ["BF1", "BD2", "BG3"]

    def test_a_benched_player_gives_his_position_number_back(self):
        """Benching the middle forward renumbers the one below him.

        Separates "the label changed" from "the count changed": if bench rows
        still consumed a position counter, the third forward would stay `F3`.
        """
        rows = main._roster_slots([_row("F"), _row("F", bench=True), _row("F")])
        assert [r["slot"] for r in rows] == ["F1", "BF1", "F2"]

    def test_a_position_past_its_starting_slots_keeps_counting(self):
        """No clamp at 12 — `F13` is the signal that someone belongs on the bench.

        Pins the owner decision. Clamping would hide the one state that needs
        acting on, and auto-benching the overflow would overwrite a choice that
        is the operator's (`/toggle-bench`, capped by `set_bench`).
        """
        rows = main._roster_slots([_row("F") for _ in range(14)])
        assert rows[11]["slot"] == "F12"
        assert rows[12]["slot"] == "F13"
        assert rows[13]["slot"] == "F14"

    def test_a_correctly_benched_full_roster_lands_exactly_on_the_shape(self):
        """12F + 6D + 2G starting and 4 benched is the CBA roster, exactly."""
        rows = main._roster_slots(
            [_row("F") for _ in range(12)]
            + [_row("D") for _ in range(6)]
            + [_row("G") for _ in range(2)]
            + [_row("F", bench=True), _row("F", bench=True),
               _row("D", bench=True), _row("G", bench=True)]
        )
        slots = [r["slot"] for r in rows]
        assert slots[11] == "F12"
        assert slots[17] == "D6"
        assert slots[19] == "G2"
        assert slots[20:] == ["BF1", "BF2", "BD3", "BG4"]

    def test_the_labels_follow_the_order_they_are_given(self):
        """It must not sort. The caller sorts; this numbers what it is handed.

        If it re-sorted, the label and the row it sits on could disagree — the
        one failure this column cannot afford.
        """
        rows = main._roster_slots([_row("G"), _row("F"), _row("D"), _row("F")])
        assert [r["slot"] for r in rows] == ["G1", "F1", "D1", "F2"]


# --------------------------------------------------------------------------
# The column, as rendered
# --------------------------------------------------------------------------


def _roster_table(html: str) -> str:
    """The active roster table only — never the Minors table below it."""
    panel = section_of(html, "team-panel")
    start = panel.index("<thead>")
    return panel[start:panel.index("</table>", start)]


def _slot_rows(html: str) -> list[tuple[str, str]]:
    """(slot, name) for every rendered roster row, in display order.

    There is no position cell to read: the slot label IS the position for a
    starter, which is why the Pos column came out on 2026-09-20.
    """
    table = _roster_table(html)
    body = table[table.index("</thead>"):]
    out = []
    for row in re.findall(r"<tr\b.*?</tr>", body, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 4:
            continue
        strip = lambda c: re.sub(r"<[^>]+>", "", c).strip()  # noqa: E731
        out.append((strip(cells[1]), strip(cells[3])))
    return out


class TestTheColumnIsWiredUp:
    def test_the_header_is_there(self, client):
        table = _roster_table(client.get(f"/team-view/{MY_TEAM}").text)
        head = table[:table.index("</thead>")]
        assert ">#<" in head, "the roster table has no # column header"

    def test_the_header_explains_itself_without_a_daisyui_bubble(self, client):
        """`title`, never `data-tip`.

        This table is a horizontal scroller, and DaisyUI centres a bubble on its
        trigger with no flip logic, so one anchored to a header in a scroller is
        clipped at essentially every scroll position. The browser suite cannot
        catch it — it measures at `scrollLeft: 0` — so this cheap static check
        stands in, the same way `#league-state`'s does.
        """
        table = _roster_table(client.get(f"/team-view/{MY_TEAM}").text)
        head = table[:table.index("</thead>")]
        assert "data-tip" not in head
        assert "Lineup slot" in head, "the # header explains nothing"

    def test_the_roster_table_has_no_position_column(self, client):
        """Removed 2026-09-20: the slot label is the position, for every starter.

        Pins the decision so the column is not reinstated by reflex. The Minors
        table below keeps its own Pos column and is deliberately untouched —
        minors carry no lineup slot to read the position off, so this asserts
        against the ROSTER table alone.
        """
        table = _roster_table(client.get(f"/team-view/{MY_TEAM}").text)
        head = table[:table.index("</thead>")]
        assert ">Pos<" not in head, (
            "the roster table has a Pos column again — the slot label already "
            "carries it"
        )
        assert ">Grp<" in head, (
            "this read the wrong table or the wrong slice — Grp should still "
            "be there, so a passing Pos assertion would mean nothing"
        )

    def test_the_first_of_each_position_is_numbered_one(self, client):
        rows = _slot_rows(client.get(f"/team-view/{MY_TEAM}").text)
        slots = [slot for slot, _ in rows if slot]
        assert slots, "no roster row carried a slot label"
        for pos in ("F", "D", "G"):
            first = [s for s in slots if s.startswith(pos) and s[1:].isdigit()]
            if first:
                assert first[0] == f"{pos}1", (
                    f"the first {pos} on the roster is labelled {first[0]}"
                )

    def test_the_numbers_run_without_a_gap(self, client):
        """F1, F2, F3 … with nothing skipped, which is what makes it a count.

        A gap would mean something consumed a counter without rendering — the
        failure mode if suggested-buy rows ever started taking numbers.
        """
        rows = _slot_rows(client.get(f"/team-view/{MY_TEAM}").text)
        for pos in ("F", "D", "G"):
            got = [s for s, _ in rows if s.startswith(pos) and s[1:].isdigit()]
            assert got == [f"{pos}{i}" for i in range(1, len(got) + 1)], (
                f"{pos} slots are not a gapless 1..N sequence: {got}"
            )

    def test_a_suggested_buy_row_carries_no_number(self, client):
        """Derived from the markup, not from a name: a target row is italic."""
        table = _roster_table(client.get(f"/team-view/{MY_TEAM}").text)
        body = table[table.index("</thead>"):]
        targets = [
            r for r in re.findall(r"<tr\b.*?</tr>", body, re.S)
            if "italic" in r.split(">", 1)[0]
        ]
        if not targets:
            pytest.skip("no MILP target rows on this state")
        for row in targets:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            assert re.sub(r"<[^>]+>", "", cells[1]).strip() == "", (
                "a suggested buy was given a lineup slot"
            )

    def test_benching_a_player_moves_him_into_the_bench_sequence(self, client):
        """Both directions, because only the change proves the flag is read.

        Asserting he ends up on a `B` slot alone would pass against a column
        that labelled everyone `B`; asserting his position count drops by one
        alone would pass against a column that simply dropped him. The pair is
        what pins it.
        """
        client.get(f"/team-view/{MY_TEAM}")
        victim = a_roster_player(MY_TEAM)
        pos = victim.position

        before = _slot_rows(client.get(f"/team-view/{MY_TEAM}").text)
        was = dict((n, s) for s, n in before)[victim.name]
        assert was.startswith(pos), (
            f"{pos} player starts on {was} — the fixture is not what it claims"
        )
        n_pos_before = sum(1 for s, _ in before if s.startswith(pos))
        n_bench_before = sum(1 for s, _ in before if s.startswith("B"))

        r = client.post(
            "/toggle-bench",
            data={"team_code": MY_TEAM, "player_name": victim.name},
        )
        assert r.status_code == 200

        after = _slot_rows(client.get(f"/team-view/{MY_TEAM}").text)
        now = dict((n, s) for s, n in after)[victim.name]
        assert now.startswith("B"), f"benched player still reads {now}"

        n_pos_after = sum(1 for s, _ in after if s.startswith(pos))
        n_bench_after = sum(1 for s, _ in after if s.startswith("B"))
        assert n_pos_after == n_pos_before - 1, (
            f"{pos} count went {n_pos_before} -> {n_pos_after}; benching must "
            f"give the position number back"
        )
        assert n_bench_after == n_bench_before + 1


def _opacity(classes: str) -> float:
    out = 1.0
    for pct in re.findall(r"\bopacity-(\d+)\b", classes):
        out *= int(pct) / 100
    return out


class TestTheBenchLabelIsReadable:
    def test_a_bench_label_is_not_dimmed_twice(self, client):
        """`opacity` multiplies down the tree: a bench row is `opacity-50` and
        the slot cell was `opacity-60`, so `BF1` rendered at 0.30. The label
        is the only thing on the row that says why it is dimmed."""
        victim = a_roster_player(MY_TEAM)
        client.post("/toggle-bench", data={"team_code": MY_TEAM, "player_name": victim.name})

        body = _roster_table(client.get(f"/team-view/{MY_TEAM}").text)
        row = next(r for r in re.findall(r"<tr\b.*?</tr>", body, re.S) if victim.name in r)
        row_class = re.match(r'<tr class="([^"]*)"', row).group(1)
        slot_cell = re.findall(r"<td([^>]*)>", row)[1]
        assert "opacity-50" in row_class, "the fixture did not bench him"
        assert _opacity(row_class) * _opacity(slot_cell) >= 0.5, (
            f"the bench label renders at {_opacity(row_class) * _opacity(slot_cell):.2f}"
        )


class TestTheRosterTableIsNotSilentlyOffset:
    """Header-to-cell agreement for the roster table.

    `#league-state` has had this since 2026-08-something and the team panel
    never did, which is how a column added to the `<thead>` and not the row —
    or the reverse — would ship: every later cell renders one column to the
    left, under the wrong heading, with nothing raising.

    Deliberately relative, like its League State sibling: it pins agreement and
    not a hard-coded 9, so the next column is a template change rather than a
    test edit.
    """

    @pytest.mark.parametrize("code", [MY_TEAM, "SRL"])
    def test_every_row_of_every_table_has_a_cell_for_every_header(self, client, code):
        """Both tables, both kinds of panel, and every row branch.

        This read BOT's roster table alone until 2026-09-22, and the grill
        found a cell dropped from an opponent's rows or from the Minors table
        surviving the whole suite, browser tests included. So every table in the
        panel is swept, on BOT's panel and an opponent's, after arranging one
        row of each kind the template branches on: a keeper, a purchase
        (`text-success`), a benched player, a minor that counts on the cap, and
        — BOT only — a MILP suggested buy (`text-info`).
        """
        team = main.auction_state.teams[code]
        assign(client, pool_top()[0], code, 1.0)
        an_eligible_minor(code=code)
        benched = next(p for p in team.keeper_players if not p.is_bench)
        client.post("/toggle-bench", data={"team_code": code, "player_name": benched.name})
        assert team.find_player(benched.name).is_bench

        panel = section_of(client.get(f"/team-view/{code}").text, "team-panel")
        tables = re.findall(r"<table\b.*?</table>", panel, re.S)
        assert len(tables) == 2, f"expected the roster and Minors tables, found {len(tables)}"
        seen = ""
        for table in tables:
            head, body = table.split("</thead>", 1)
            headers = len(re.findall(r"<th[\s>]", head))
            assert headers, "a table rendered no headers at all"
            rows = re.findall(r"<tr\b.*?</tr>", body, re.S)
            assert rows, "a table rendered no rows — this checks nothing"
            for row in rows:
                seen += row.split(">", 1)[0]
                # Counted ONCE and reused: re-running the regex inside the
                # f-string is how this message once reported 5 cells for an
                # 8-cell row — the inline copy was escaped differently.
                cells = len(re.findall(r"<td[\s>]", row))
                assert cells == headers, (
                    f"{headers} headers but {cells} cells — a column is silently "
                    f"offset:\n{row[:200]}"
                )
        for marker in ("text-success", "opacity-50"):
            assert marker in seen, f"no row carried {marker} — a branch went unchecked"
        if code == MY_TEAM:
            assert "text-info" in seen, "no suggested-buy row — a branch went unchecked"
