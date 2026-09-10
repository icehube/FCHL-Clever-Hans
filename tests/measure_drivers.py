"""What the price model's drivers are actually doing, across the whole pool.

NOT a test — pytest ignores it. Named `measure_drivers.py` for the same reason
`measure_spend.py`, `measure_layout.py` and `measure_ceiling.py` are: an
instrument, not an assertion.

It imports `data_loader` and `price_model` and **nothing else** — no `main`, no
`STATE_DIR`, no `TestClient` — so like `measure_spend.py` it structurally cannot
touch a live draft, which is a stronger guarantee than the temp-dir redirect
`measure_layout.py` relies on. `tests/test_measure_drivers.py` pins that.

It exists because the chart card answers "why is THIS player priced there" one
player at a time, and the question that decides whether the pricer notebook
needs a refit is "how many others, and by how much". Running it is what turns
"Panarin looks wrong" into a count and a dollar figure.

    .venv/bin/python -m tests.measure_drivers [--position F] [--top 20]
                                              [--pairs 12]
"""

import argparse
import math
from collections import defaultdict

import data_loader
from price_model import (
    DRIVER_GROUPS,
    compute_reference_features,
    decompose_player,
    load_model_params,
    points_slopes,
    predict_all_prices,
)

POSITIONS = ("F", "D", "G")


def load_pool():
    """The draft-time pool and its frozen reference, from the CSV alone."""
    params = load_model_params()
    _, biddable = data_loader.load_players(
        team_odds=data_loader.load_team_odds(),
        goalie_wins=data_loader.load_goalie_wins(),
    )
    return biddable, params, compute_reference_features(biddable, params)


def slope_table(params: dict) -> list[tuple[str, float, float, bool]]:
    """(position, breakpoint, slope, is_negative) for every piecewise segment."""
    return [
        (pos, bp, slope, slope < 0.0)
        for pos in POSITIONS
        for bp, slope in points_slopes(params[pos])
    ]


def production(player, params: dict) -> float:
    """The production input this position is actually PRICED on.

    Skaters: projected points. Goalies: projected WINS — `coef_projected_points`
    is 0.0 for G, so ranking goalies by their 2W+3SO composite and then reading
    a wins-driven factor compares two different inputs and manufactures 355
    "inversions" out of nothing. That was the first version of this file.
    """
    if player.position != "G":
        return float(player.projected_points)
    if player.proj_wins is not None:
        return float(player.proj_wins)
    return player.projected_points / params["metadata"]["goalie_pts_per_win"]


def price_inversions(pool, params, refs, position: str) -> list[tuple]:
    """Same-position pairs where MORE projected points costs LESS overall.

    **This is not by itself a defect, and reading it as one is the trap.** The
    model has five drivers, so a lower-scoring player on a better team with a
    bigger reputation SHOULD cost more. Measured: D has 898 of these with a
    points slope that is positive at every segment. Reported because it is
    what the owner actually sees on the panel, but the diagnosis lives in
    `points_inversions` below, which holds the other four drivers out.
    """
    preds = predict_all_prices(pool, params)
    players = sorted(
        (p for p in pool.values() if p.position == position),
        key=lambda p: -production(p, params),
    )
    out = []
    for i, better in enumerate(players):
        for worse in players[i + 1:]:
            if production(better, params) <= production(worse, params):
                continue
            gap = preds[worse.name].expected_price - preds[better.name].expected_price
            if gap > 0:
                out.append((better, worse, gap,
                            preds[better.name].expected_price,
                            preds[worse.name].expected_price))
    out.sort(key=lambda r: -r[2])
    return out


def points_inversions(pool, params, refs, position: str) -> list[tuple]:
    """Pairs where more points earns a SMALLER points contribution.

    The defect, isolated. Compares the Points driver alone, so team quality,
    reputation, scarcity and contract status cannot mask or manufacture one:
    a hit here means the fit itself pays less for more production. Zero for D
    and G, which is what makes it a diagnosis rather than a description.
    """
    def points_factor(player):
        b = decompose_player(player, params, refs[player.position])
        return next(d for d in b.drivers if d.group == "Points").factor

    players = sorted(
        (p for p in pool.values() if p.position == position),
        key=lambda p: -production(p, params),
    )
    factors = {p.name: points_factor(p) for p in players}
    out = []
    for i, better in enumerate(players):
        for worse in players[i + 1:]:
            if production(better, params) <= production(worse, params):
                continue
            gap = factors[worse.name] - factors[better.name]
            if gap > 1e-12:
                out.append((better, worse, gap,
                            factors[better.name], factors[worse.name]))
    out.sort(key=lambda r: -r[2])
    return out


def driver_mix(pool, params, refs) -> dict:
    """Mean |log effect| per driver per position, and how often each is largest.

    This is what says the entry's three named drivers were not the whole story:
    `log_rank` dominates, and it is computed FROM projected points.
    """
    totals = defaultdict(lambda: defaultdict(list))
    largest = defaultdict(lambda: defaultdict(int))
    for player in pool.values():
        ref = refs.get(player.position)
        if not ref:
            continue
        b = decompose_player(player, params, ref)
        for d in b.drivers:
            totals[player.position][d.group].append(abs(d.log_delta))
        top = max(b.drivers, key=lambda d: abs(d.log_delta))
        largest[player.position][top.group] += 1
    return {"mean": totals, "largest": largest}


def hinge_cost(pool, params, refs) -> tuple[list, float]:
    """What the negative F hinge above 80 pts costs, player by player.

    Re-prices each affected forward with `coef_pts_hinge_80` zeroed — the
    smallest change that removes the inversion — and reports the difference.
    The total is the refit's business case in one number.
    """
    import copy

    neutral = copy.deepcopy(params)
    neutral["F"]["coef_pts_hinge_80"] = 0.0
    live = predict_all_prices(pool, params)
    without = predict_all_prices(pool, neutral)

    rows = [
        (p, live[p.name].expected_price, without[p.name].expected_price)
        for p in pool.values()
        if p.position == "F" and p.projected_points > 80
    ]
    rows.sort(key=lambda r: -r[0].projected_points)
    return rows, sum(w - l for _, l, w in rows)


def report(position: str = "F", top: int = 20, pairs: int = 12) -> None:
    pool, params, refs = load_pool()
    preds = predict_all_prices(pool, params)

    print(f"\n=== pool: {len(pool)} biddable, from {data_loader.PLAYERS_CSV}\n")

    print("--- reference (the 'typical' player each breakdown measures against)")
    for pos in POSITIONS:
        r = refs.get(pos)
        if not r:
            continue
        from price_model import _score, _sigmoid
        logit, log_mu = _score(params[pos], r)
        print(f"  {pos}: ${math.exp(log_mu):5.2f}M  p_floor {_sigmoid(logit):.3f}  "
              f"{r['projected_points']:.0f} pts, rank {math.exp(r['log_rank']):.0f}, "
              f"lag {'yes' if r['has_lag'] else 'none'}")

    print("\n--- points slope per segment (log $ per point)")
    for pos, bp, slope, bad in slope_table(params):
        flag = "   !! NEGATIVE — more points costs less" if bad else ""
        print(f"  {pos} {bp:5.0f}+ pts : {slope:+.4f}{flag}")

    print(f"\n--- {position} price vs points at the reference (rank 10, $6M lag)")
    from price_model import predict_price
    peak_pts, peak_price = None, -1.0
    curve = []
    for pts in range(0, 141, 10):
        price = predict_price(position, pts, 5.0, False, params,
                              last_salary=6.0, pos_rank=10).expected_price
        curve.append((pts, price))
        if price > peak_price:
            peak_pts, peak_price = pts, price
    for pts, price in curve:
        mark = "  <-- peak" if pts == peak_pts else ""
        print(f"  {pts:4} pts : ${price:5.2f}M{mark}")

    print("\n--- driver mix (mean |log effect|, and how often each is largest)")
    mix = driver_mix(pool, params, refs)
    groups = [g for g, _ in DRIVER_GROUPS]
    print(f"  {'pos':4}" + "".join(f"{g:>13}" for g in groups))
    for pos in POSITIONS:
        if pos not in mix["mean"]:
            continue
        means = "".join(
            f"{sum(mix['mean'][pos][g]) / max(len(mix['mean'][pos][g]), 1):13.3f}"
            for g in groups
        )
        print(f"  {pos:4}{means}")
        n = sum(mix["largest"][pos].values()) or 1
        share = "".join(f"{100 * mix['largest'][pos][g] / n:12.0f}%" for g in groups)
        print(f"  {'':4}{share}   <- share of players where it is the largest")

    print(f"\n--- top {top} by expected price")
    ranked = sorted(pool.values(), key=lambda p: -preds[p.name].expected_price)[:top]
    print(f"  {'player':24}{'pos':4}{'pts':>5}{'rank':>6}{'lag':>6}"
          + "".join(f"{g[:9]:>10}" for g in groups) + f"{'E[$]':>8}")
    for p in ranked:
        ref = refs.get(p.position)
        if not ref:
            continue
        b = decompose_player(p, params, ref)
        facs = "".join(f"{d.factor:10.2f}" for d in b.drivers)
        print(f"  {p.name[:23]:24}{p.position:4}{p.projected_points:5}"
              f"{p.pos_rank:6}{p.salary:6.1f}{facs}"
              f"{preds[p.name].expected_price:8.2f}")

    print(f"\n--- {position} pairs where more points earns a SMALLER points")
    print("    contribution. This one IS the defect — the other four drivers")
    print("    are held out, so nothing else can mask or manufacture it.")
    pinv = points_inversions(pool, params, refs, position)
    print(f"  {len(pinv)} inverted pairs")
    for better, worse, gap, fb, fw in pinv[:pairs]:
        print(f"  {better.name[:20]:21} {better.projected_points:4}pts x{fb:5.2f}"
              f"   <   {worse.name[:20]:21} {worse.projected_points:4}pts x{fw:5.2f}")

    print(f"\n--- {position} pairs where more points costs less OVERALL")
    print("    NOT by itself a defect: a lesser scorer on a better team with a")
    print("    bigger reputation should cost more. Shown because it is what the")
    print("    panel actually displays.")
    inv = price_inversions(pool, params, refs, position)
    print(f"  {len(inv)} inverted pairs")
    for better, worse, gap, pb, pw in inv[:pairs]:
        print(f"  {better.name[:20]:21} {better.projected_points:4}pts ${pb:5.2f}M"
              f"   <   {worse.name[:20]:21} {worse.projected_points:4}pts ${pw:5.2f}M"
              f"   (gap ${gap:.2f}M)")

    print("\n--- cost of the negative F hinge above 80 pts")
    rows, total = hinge_cost(pool, params, refs)
    for p, live, without in rows:
        print(f"  {p.name[:23]:24}{p.projected_points:5}pts  "
              f"${live:5.2f}M  ->  ${without:5.2f}M without the hinge  "
              f"({without - live:+.2f}M)")
    print(f"  {len(rows)} forwards, ${total:.1f}M of model price suppressed in total")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--position", default="F", choices=POSITIONS)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--pairs", type=int, default=12)
    a = ap.parse_args()
    report(position=a.position, top=a.top, pairs=a.pairs)
