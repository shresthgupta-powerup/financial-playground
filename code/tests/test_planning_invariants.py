"""Corpus-wide financial-invariant harness (Plan 207 Ph2, D-P207-6).

Launch-audit regression gate for the financial-planning engine. Runs real-client
configs from the generated Advisory corpus (`tests/advisory_corpus.py`, Ph1) and
asserts invariants that must hold for ANY input — independent of the golden
master (which only pins engine == engine-at-baseline):

  I1  Payout-split identity: gross Replenishing Payouts == Investment Used for
      Payouts + Net Payouts (Pool), every month (exact netting identity).
  I2  Non-negative balances: Core Corpus / Debt Pool / Hybrid Pool / per-goal
      tranche values never go negative on a successful run.
  I3  Withdrawal completeness: success=True implies every withdrawal row is
      fully funded with zero shortfall.
  I4  Goal-chain delivery: each Non-replenishing tranche's chain delivers the
      tranche FV (goal rows sum to the expanded tranche amount).
  I5  Solver minimality: the found retirement date succeeds AND one month
      earlier fails (earliest-feasible contract; skipped when the solver
      already lands on current_date).
  I6  Conservation of money (zero-return / zero-tax): with all returns and
      taxes zeroed, month-over-month change in total wealth equals
      investment-to-corpus + one-time investments − goal payments − net
      payouts, exactly. Returns/taxes are the only legitimate wealth sources
      and sinks the model has beyond these flows.
  I7  Snapshot consistency: the service snapshot equals the comprehensive
      view's value columns at the first month-end >= retirement, and equals
      the matching wealth_monthly row.

The full 25-client x 3-profile sweep ran as the one-time Plan 207 audit
(results in `financial_plan_audit.md`); this module keeps a representative
subset green in CI for runtime reasons (solves are seconds each).
"""

import numpy as np
import pandas as pd
import pytest

from app.planning.engine import (
    _DEFAULT_INSTRUMENT_PARAMS,
    expand_recurring_goal_to_tranches,
    find_retirement_date,
    run_simulation,
)
from app.planning.service import _build_snapshot, _build_wealth_monthly
from tests.advisory_corpus import ADVISORY_CLIENTS, build_config, total_goal_pv

# ---------------------------------------------------------------------------
# Representative client selection (deterministic — survives corpus regeneration)
# ---------------------------------------------------------------------------


def _select_representative_clients():
    """Smallest stable set covering every (nature, structure, end_mode) shape,
    plus the largest client (most goals) as the stress case."""
    chosen, covered = [], set()
    for key in sorted(ADVISORY_CLIENTS):
        shapes = {(g["nature"], g["structure"], g.get("end_mode"))
                  for g in ADVISORY_CLIENTS[key]}
        if shapes - covered:
            chosen.append(key)
            covered |= shapes
    biggest = max(sorted(ADVISORY_CLIENTS), key=lambda k: len(ADVISORY_CLIENTS[k]))
    if biggest not in chosen:
        chosen.append(biggest)
    return chosen


REP_CLIENTS = _select_representative_clients()

_ZERO_PARAMS = {
    bucket: {"return": 0.0, "stcg_tax": 0.0, "ltcg_tax": 0.0}
    for bucket in _DEFAULT_INSTRUMENT_PARAMS
}


# ---------------------------------------------------------------------------
# Shared solve cache — one solver + simulation pass per representative client
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def solved():
    """{client_key: dict} — solver result + full simulation outputs (moderate)."""
    out = {}
    for key in REP_CLIENTS:
        cfg = build_config(key, "moderate")
        res = find_retirement_date(cfg)
        entry = {"config": cfg, "result": res}
        if res["success"]:
            success, ft, failure, pm, goal_dfs, comp = run_simulation(
                cfg, res["retirement_date"], _DEFAULT_INSTRUMENT_PARAMS
            )
            entry.update(success=success, final_trans=ft, goal_dfs=goal_dfs,
                         comprehensive=comp)
        out[key] = entry
    return out


# ---------------------------------------------------------------------------
# Invariant helpers (importable by the one-time audit sweep script)
# ---------------------------------------------------------------------------


def check_payout_split_identity(comp):
    """I1: gross payouts == investment-used + pool-funded, rowwise."""
    gross = comp["Replenishing Payouts"].to_numpy(dtype=float)
    used = comp["Investment Used for Payouts"].to_numpy(dtype=float)
    pool = comp["Net Payouts (Pool)"].to_numpy(dtype=float)
    bad = ~np.isclose(gross, used + pool, rtol=1e-9, atol=1e-6)
    return [
        f"{comp['Date'].iloc[i].date()}: gross {gross[i]:.2f} != "
        f"used {used[i]:.2f} + pool {pool[i]:.2f}"
        for i in np.where(bad)[0]
    ]


def check_non_negative_balances(comp, atol=1e-6):
    """I2: no *Value column may go negative."""
    problems = []
    for col in (c for c in comp.columns if c.endswith("Value")):
        vals = comp[col].to_numpy(dtype=float)
        worst = np.nanmin(vals) if len(vals) else 0.0
        if worst < -atol:
            i = int(np.nanargmin(vals))
            problems.append(f"{col} = {worst:.2f} at {comp['Date'].iloc[i].date()}")
    return problems


def check_withdrawals_funded(final_trans):
    """I3: success implies every withdrawal fully funded, zero shortfall."""
    problems = []
    if "fully_funded" in final_trans.columns:
        bad = final_trans[final_trans["fully_funded"] == False]  # noqa: E712
        problems += [f"unfunded: {r['Description']} on {r['Date']}" for _, r in bad.iterrows()]
    if "shortfall" in final_trans.columns:
        sh = final_trans[final_trans["shortfall"].fillna(0).abs() > 1e-6]
        problems += [f"shortfall {r['shortfall']:.2f}: {r['Description']} on {r['Date']}"
                     for _, r in sh.iterrows()]
    return problems


def check_goal_chain_delivery(config, goal_dfs, retirement_date, rtol=1e-6):
    """I4: each tranche's chain goal-rows sum to the expanded tranche FV."""
    current_date = pd.Timestamp(config["current_date"])
    expected = {}
    for goal in config.get("goals", []) or []:
        if str(goal.get("nature", "")).lower() == "replenishing":
            continue
        g = dict(goal)
        if g.get("start_date_mode") == "At retirement":
            g["start_date"] = retirement_date
        tranches = expand_recurring_goal_to_tranches(g, current_date)
        for i, (_, fv) in enumerate(tranches):
            label = g["name"] if len(tranches) == 1 else f"{g['name']} ({i+1}/{len(tranches)})"
            expected[label] = fv
    problems = []
    for label, df in goal_dfs.items():
        delivered = float(df.loc[df["place"].str.lower() == "goal", "inflow_amount"].sum())
        fv = expected.get(label)
        if fv is None:
            problems.append(f"unexpected chain label {label!r}")
        elif not np.isclose(delivered, fv, rtol=rtol, atol=1.0):
            problems.append(f"{label}: chain delivers {delivered:.2f}, tranche FV {fv:.2f}")
    return problems


def check_conservation_zero_return(config, retirement_date):
    """I6: zero-return/zero-tax — Δ(total wealth) == flows, every month.

    Returns (problems, success_flag). Caller must ensure the config is funded
    enough to succeed under zero growth (conservation needs a completed run).
    """
    success, ft, failure, pm, goal_dfs, comp = run_simulation(
        config, retirement_date, _ZERO_PARAMS
    )
    if not success or comp.empty:
        return [], False

    value_cols = [c for c in comp.columns if c.endswith("Value")]
    total = comp[value_cols].sum(axis=1).to_numpy(dtype=float)
    months = comp["Date"].dt.to_period("M")

    # Goal payments by calendar month (chain terminal rows).
    goal_pay = {}
    for df in goal_dfs.values():
        rows = df[df["place"].str.lower() == "goal"]
        for _, r in rows.iterrows():
            m = pd.Timestamp(r["inflow_date"]).to_period("M")
            goal_pay[m] = goal_pay.get(m, 0.0) + float(r["inflow_amount"])

    # One-time investments by calendar month.
    one_time = {}
    for w in config.get("one_time_investments", []) or []:
        m = pd.Timestamp(w["date"]).to_period("M")
        one_time[m] = one_time.get(m, 0.0) + float(w.get("amount", 0))

    inv_corpus = comp["Investment to Corpus"].to_numpy(dtype=float)
    net_pay = comp["Net Payouts (Pool)"].to_numpy(dtype=float)

    problems = []
    for i in range(1, len(comp)):
        m = months.iloc[i]
        expected_delta = (inv_corpus[i] + one_time.get(m, 0.0)
                          - goal_pay.get(m, 0.0) - net_pay[i])
        actual_delta = total[i] - total[i - 1]
        if not np.isclose(actual_delta, expected_delta, rtol=1e-7, atol=2.0):
            problems.append(
                f"{comp['Date'].iloc[i].date()}: Δwealth {actual_delta:.2f} != "
                f"flows {expected_delta:.2f} (inv {inv_corpus[i]:.2f}, "
                f"goal {goal_pay.get(m, 0.0):.2f}, pool {net_pay[i]:.2f})"
            )
    return problems, True


def check_snapshot_consistency(comp, retirement_date, config):
    """I7: service snapshot == comprehensive value columns == wealth_monthly row."""
    problems = []
    snapshot = _build_snapshot(comp, retirement_date)
    if snapshot is None:
        return ["snapshot is None for a successful run"]

    df = comp.copy()
    df["Date"] = pd.to_datetime(df["Date"])
    row = df[df["Date"] >= retirement_date].head(1).iloc[0]
    value_cols = [c for c in comp.columns if c.endswith("Value")]
    expected_total = float(sum(row.get(c, 0) or 0 for c in value_cols))
    if not np.isclose(snapshot["total"], expected_total, rtol=1e-9, atol=0.02):
        problems.append(f"snapshot total {snapshot['total']} != comprehensive {expected_total:.2f}")
    parts = (snapshot["core"] + snapshot["debt"] + snapshot["hybrid"]
             + snapshot["goal_debt"] + snapshot["goal_hybrid"])
    if not np.isclose(snapshot["total"], parts, rtol=1e-9, atol=0.05):
        problems.append(f"snapshot total {snapshot['total']} != sum of parts {parts:.2f}")

    current_date = pd.Timestamp(config["current_date"])
    death = current_date + pd.DateOffset(
        years=int(config["target_lifetime"] - config["current_age"]))
    wm = _build_wealth_monthly(comp, death)
    snap_date = row["Date"].strftime("%Y-%m-%d")
    match = [r for r in wm if r["date"] == snap_date]
    if not match:
        problems.append(f"no wealth_monthly row for snapshot date {snap_date}")
    elif not np.isclose(match[0]["total"], snapshot["total"], rtol=1e-9, atol=0.02):
        problems.append(
            f"wealth_monthly total {match[0]['total']} != snapshot {snapshot['total']}")
    return problems


# ---------------------------------------------------------------------------
# Tests — representative subset (full sweep documented in financial_plan_audit.md)
# ---------------------------------------------------------------------------


class TestCorpusInvariants:
    def test_representative_set_covers_all_goal_shapes(self):
        shapes = {(g["nature"], g["structure"], g.get("end_mode"))
                  for k in REP_CLIENTS for g in ADVISORY_CLIENTS[k]}
        all_shapes = {(g["nature"], g["structure"], g.get("end_mode"))
                      for gs in ADVISORY_CLIENTS.values() for g in gs}
        assert shapes == all_shapes

    @pytest.mark.parametrize("client", REP_CLIENTS)
    def test_payout_split_identity(self, solved, client):
        entry = solved[client]
        if not entry["result"]["success"]:
            pytest.skip("infeasible under moderate profile")
        assert check_payout_split_identity(entry["comprehensive"]) == []

    @pytest.mark.parametrize("client", REP_CLIENTS)
    def test_non_negative_balances(self, solved, client):
        entry = solved[client]
        if not entry["result"]["success"]:
            pytest.skip("infeasible under moderate profile")
        assert check_non_negative_balances(entry["comprehensive"]) == []

    @pytest.mark.parametrize("client", REP_CLIENTS)
    def test_withdrawals_fully_funded(self, solved, client):
        entry = solved[client]
        if not entry["result"]["success"]:
            pytest.skip("infeasible under moderate profile")
        assert entry["success"] is True
        assert check_withdrawals_funded(entry["final_trans"]) == []

    @pytest.mark.parametrize("client", REP_CLIENTS)
    def test_goal_chains_deliver_tranche_fv(self, solved, client):
        entry = solved[client]
        if not entry["result"]["success"]:
            pytest.skip("infeasible under moderate profile")
        assert check_goal_chain_delivery(
            entry["config"], entry["goal_dfs"], entry["result"]["retirement_date"]
        ) == []

    @pytest.mark.parametrize("client", REP_CLIENTS)
    def test_snapshot_consistency(self, solved, client):
        entry = solved[client]
        if not entry["result"]["success"]:
            pytest.skip("infeasible under moderate profile")
        assert check_snapshot_consistency(
            entry["comprehensive"], entry["result"]["retirement_date"], entry["config"]
        ) == []

    @pytest.mark.parametrize("client", REP_CLIENTS[:3])
    def test_solver_minimality(self, solved, client):
        entry = solved[client]
        if not entry["result"]["success"]:
            pytest.skip("infeasible under moderate profile")
        cfg, ret = entry["config"], entry["result"]["retirement_date"]
        if pd.Timestamp(ret) <= pd.Timestamp(cfg["current_date"]):
            pytest.skip("solver landed on current_date — no earlier month exists")
        earlier = pd.Timestamp(ret) - pd.DateOffset(months=1)
        success_earlier, *_ = run_simulation(cfg, earlier, _DEFAULT_INSTRUMENT_PARAMS)
        assert success_earlier is False, (
            f"{client}: retirement {ret} is not minimal — {earlier.date()} also succeeds"
        )

    @pytest.mark.parametrize("client", REP_CLIENTS[:3])
    def test_conservation_zero_return(self, client):
        # Abundant corpus so the run completes under zero growth; retirement
        # fixed (+5y) — conservation is about accounting, not feasibility.
        cfg = build_config(client, "wealthy")
        cfg["current_corpus"] = 10.0 * total_goal_pv(cfg["goals"])
        ret = pd.Timestamp(cfg["current_date"]) + pd.DateOffset(years=5)
        problems, ran = check_conservation_zero_return(cfg, ret)
        assert ran, f"{client}: zero-return run did not complete even with 10x corpus"
        assert problems == []
