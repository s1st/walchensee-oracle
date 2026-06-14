"""Scoring protocol for the ML ceiling spike (Phase C).

The numbers this module produces are the contract the rule baseline and
the ML model are both scored against (per the research doc, §4–§5 and the
handoff). All formulas have a primary-source citation in the docstring
so the Phase E writeup can re-derive them.

Conventions:
- All inputs are NumPy arrays. Categorical y_true uses LABEL_TO_INT
  ordering (0=go, 1=maybe, 2=no_go) from `dataset`. Probability vectors
  y_proba have shape (N, 3) in the same column order.
- The "binary thermal" target is GO-or-MAYBE = 1, NO_GO = 0. It feeds the
  probabilistic metrics only (Brier + relative-value curve). The reported
  `peirce`/`hss` fields are the 3-class Hanssen-Kuipers / Heidke scores on
  the full confusion (rule = +0.066 on the 1,912-row replay). The research
  doc's §4.1 +0.107 figure is a *binary* Peirce anchor on its own dataset
  and is not directly comparable to the 3-class numbers reported here.
- For the rule baseline, the comparison is on its categorical `overall`
  field — categorical-vs-categorical scoring (HSS, accuracy, hard-error
  rate) but not the probabilistic metrics (RPS, Brier, value curve).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

import numpy as np

from oracle.calibration import (
    McNemarResult,
    heidke_skill_score,
    mcnemar,
    mean_cost,
    peirce_skill_score,
)
from oracle.knowledge.rules import Signal
from oracle.ml.dataset import (
    LABEL_ORDER,
    LABEL_TO_INT,
    ReplayDataset,
    binarise_thermal,
)


# --- categorical metrics (multi-class) ------------------------------------


def _confusion_3x3(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, dict[str, int]]:
    """3-class confusion dict in the same shape as `calibration._empty_confusion`."""
    confusion: dict[str, dict[str, int]] = {f: {a: 0 for a in LABEL_ORDER} for f in LABEL_ORDER}
    for t, p in zip(y_true, y_pred, strict=True):
        ts, ps = LABEL_ORDER[int(t)], LABEL_ORDER[int(p)]
        confusion[ps][ts] += 1
    return confusion


def hard_error_rate(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Fraction of days the forecast was maximally wrong (GO↔NO_GO, skipping MAYBE).

    A "hard error" is a categorical misfire that would change a rider's
    decision by the maximum amount — the research doc §4.1 and the
    historical baseline's "6.3% hard-error rate" are both built off this.
    """
    go = int(LABEL_TO_INT[Signal.GO.value])
    nogo = int(LABEL_TO_INT[Signal.NO_GO.value])
    wrong = ((y_true == go) & (y_pred == nogo)) | ((y_true == nogo) & (y_pred == go))
    return float(wrong.mean()) if len(wrong) else 0.0


def multiclass_accuracy(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    if len(y_true) == 0:
        return 0.0
    return float((y_true == y_pred).mean())


# --- probabilistic metrics (3-class) --------------------------------------


def rps_score(y_true: np.ndarray, y_proba: np.ndarray) -> float:
    """Ranked Probability Score for an ordinal 3-class target.

    RPS = (1/N) × Σ_n Σ_k (CDF_pred(k) − CDF_obs(k))²
    where CDF_pred(k) = Σ_{j<=k} P(class j) and CDF_obs(k) = 1{y_obs <= k}.

    Ranges 0 (perfect) to 1 (worst). The probabilistic counterpart to
    the Gerrity score (research doc §4.3, citing the WMO/WWRP FAQ and
    Wilks *Statistical Methods in the Atmospheric Sciences* Ch. 8).
    """
    if len(y_true) == 0:
        return 0.0
    n_classes = y_proba.shape[1]
    # Cumulative probability columns. cumsum gives the CDF directly for
    # the class order (GO, MAYBE, NO_GO).
    cdf_pred = np.cumsum(y_proba, axis=1)
    cdf_obs = np.zeros_like(cdf_pred)
    # 1{y_obs <= k} — equivalent to broadcasting the indicator across k.
    classes = np.arange(n_classes)
    cdf_obs = (classes[None, :] >= y_true[:, None]).astype(float)
    return float(((cdf_pred - cdf_obs) ** 2).sum(axis=1).mean())


# --- Brier score with Murphy (1973) decomposition -------------------------
# BS = REL − RES + UNC, where:
#   REL = (1/N) Σ_k N_k (p̄_k − ō_k)²   (reliability / calibration, 0 at perfect)
#   RES = (1/N) Σ_k N_k (ō_k − ō)²       (resolution / discrimination, ≥ 0)
#   UNC = ō (1 − ō)                      (inherent climatological uncertainty)
# Bins the forecast into K equal-width bins on [0, 1]; skips empty bins.


@dataclass
class BrierDecomposition:
    bs: float
    rel: float
    res: float
    unc: float

    def as_dict(self) -> dict[str, float]:
        return {"bs": self.bs, "rel": self.rel, "res": self.res, "unc": self.unc}


def brier_decomposition(
    y_true_bin: np.ndarray,
    y_prob_bin: np.ndarray,
    n_bins: int = 10,
) -> BrierDecomposition:
    """Brier score with Murphy (1973) decomposition on a binary event.

    Research doc §4.3: Brier (1950) for the score, Murphy (1973) for the
    decomposition. The sign convention used here is the standard one:
    REL = 0 for perfect calibration, RES = positive for better-than-
    climatology discrimination, UNC = inherent uncertainty.
    """
    y_true_bin = np.asarray(y_true_bin).astype(int)
    y_prob_bin = np.asarray(y_prob_bin, dtype=float)
    if len(y_true_bin) == 0:
        return BrierDecomposition(0.0, 0.0, 0.0, 0.0)
    n = len(y_true_bin)
    bs = float(((y_prob_bin - y_true_bin) ** 2).mean())
    o_bar = float(y_true_bin.mean())
    unc = o_bar * (1.0 - o_bar)
    # Bin forecasts in [0, 1] into n_bins equal-width bins. Clip to [0, 1]
    # to handle the (very rare) floating-point overshoot.
    probs_clipped = np.clip(y_prob_bin, 0.0, 1.0)
    bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
    bin_idx = np.digitize(probs_clipped, bin_edges) - 1
    bin_idx = np.clip(bin_idx, 0, n_bins - 1)
    rel = 0.0
    res = 0.0
    for k in range(n_bins):
        in_bin = bin_idx == k
        n_k = int(in_bin.sum())
        if n_k == 0:
            continue
        p_bar_k = float(probs_clipped[in_bin].mean())
        o_bar_k = float(y_true_bin[in_bin].mean())
        rel += n_k * (p_bar_k - o_bar_k) ** 2
        res += n_k * (o_bar_k - o_bar) ** 2
    return BrierDecomposition(
        bs=bs,
        rel=rel / n,
        res=res / n,
        unc=unc,
    )


# --- relative economic value (CAWCR framework) ----------------------------
# For each cost-loss ratio r ∈ (0, 1), the user acts iff forecast p > r.
# At threshold p* = r (optimal for a calibrated forecast), the 2x2 table
# at that threshold yields the expense-based value score
#
#   V = (E_climatology − E_forecast) / (E_climatology − E_perfect)
#
# where (research doc §4.4, citing Wilks *Statistical Methods* Ch. 8):
#   E_forecast    = C × P(act) + L × P(miss)   at threshold p* = r
#   E_climatology = min(C, p̄ × L)              best of always-act / never-act
#   E_perfect     = p̄ × C                      only act on event days
#
# V ∈ (−∞, 1] with V=1 for a perfect forecast, V=0 for climatology, V<0
# for worse-than-climatology. C = r × L (we work in units where L = 1).
# This normalization is what the dashboard's value-curve plot wants; the
# unnormalized CAWCR score can exceed 1.0 for perfect probabilistic
# forecasts because of how it weights the C/L ratio.


def relative_value_curve(
    y_true_bin: np.ndarray,
    y_prob_bin: np.ndarray,
    cl_ratios: Iterable[float] = np.arange(0.05, 1.0, 0.05),
) -> dict[float, float]:
    """Expense-based relative value V at each C/L ratio for a binary event.

    Returns a dict {C/L: V}. The area under the curve is the headline
    summary (research doc §4.4). Skips r values that would divide by zero
    (r = 1, or p̄ ∈ {0, 1}).
    """
    y_true_bin = np.asarray(y_true_bin).astype(int)
    y_prob_bin = np.asarray(y_prob_bin, dtype=float)
    if len(y_true_bin) == 0:
        return {r: 0.0 for r in cl_ratios}
    p_bar = float(y_true_bin.mean())
    if p_bar == 0.0 or p_bar == 1.0:
        # No signal in the labels → V undefined; return 0 across the sweep.
        return {r: 0.0 for r in cl_ratios}
    n = len(y_true_bin)
    out: dict[float, float] = {}
    for r in cl_ratios:
        if r >= 1.0 or r <= 0.0:
            continue
        C = r               # in units of L = 1
        L = 1.0
        acts = y_prob_bin > r
        hits = int(((y_true_bin == 1) & acts).sum()) / n
        false_alarms = int(((y_true_bin == 0) & acts).sum()) / n
        misses = int(((y_true_bin == 1) & ~acts).sum()) / n
        e_forecast = C * (hits + false_alarms) + L * misses
        e_climatology = min(C, p_bar * L)
        e_perfect = p_bar * C
        denom = e_climatology - e_perfect
        if denom == 0.0:
            # Climatology IS perfect — the value score is undefined; treat
            # as 0 so the curve doesn't blow up. (Happens when C/L = p̄.)
            out[float(r)] = 0.0
        else:
            out[float(r)] = float((e_climatology - e_forecast) / denom)
    return out


def area_under_value_curve(values: dict[float, float]) -> float:
    """Trapezoidal integral of V over the C/L sweep. Single-number
    summary of the value curve (research doc §4.4)."""
    if not values:
        return 0.0
    rs = sorted(values.keys())
    vs = [values[r] for r in rs]
    return float(np.trapezoid(vs, rs))


# --- per-model scoring aggregator -----------------------------------------


@dataclass
class ModelScore:
    """Scoring output for one model on one dataset split.

    For the rule baseline (categorical only), `proba` is None and the
    probabilistic metrics are absent. For the ML model, `proba` is the
    (N, 3) probability matrix and all metrics are populated.
    """
    name: str
    n: int
    accuracy: float
    peirce: float
    hss: float
    hard_error_rate: float
    mean_cost: float
    proba: np.ndarray | None = None
    rps: float | None = None
    brier: BrierDecomposition | None = None
    value_curve: dict[float, float] = field(default_factory=dict)
    # 0.0 is the "not computed" sentinel: the rule baseline has no proba, so
    # its value curve/AUC is never computed. Don't read rule value_auc=0.0 as
    # "computed and zero" — see the writeup table footnote.
    value_auc: float = 0.0

    def as_dict(self) -> dict:
        out = {
            "name": self.name,
            "n": self.n,
            "accuracy": self.accuracy,
            "peirce": self.peirce,
            "hss": self.hss,
            "hard_error_rate": self.hard_error_rate,
            "mean_cost": self.mean_cost,
            "value_auc": self.value_auc,
        }
        if self.proba is not None:
            out["rps"] = self.rps
            if self.brier is not None:
                out["brier"] = self.brier.as_dict()
        return out


def _mean_cost_3x3(confusion: dict[str, dict[str, int]]) -> float:
    """Average per-day cost under the rule baseline's missed-session /
    wasted-drive matrix. Thin wrapper around the existing `mean_cost` so
    the ML and rule reports use the same formula and the same numbers.
    """
    return mean_cost(confusion)


def score_predictions(
    name: str,
    y_true_int: np.ndarray,
    y_pred_int: np.ndarray,
    y_proba: np.ndarray | None = None,
) -> ModelScore:
    """Score a single model's predictions against the test target.

    `y_pred_int` is always required (categorical). `y_proba` is optional;
    when present, the probabilistic metrics (RPS, Brier+Murphy, value
    curve) are populated. For the rule baseline, pass `y_proba=None`.
    """
    n = len(y_true_int)
    confusion = _confusion_3x3(y_true_int, y_pred_int)
    y_bin = binarise_thermal(y_true_int)  # binary target — used for Brier + value curve below
    # Peirce + HSS are scored on the full 3-class confusion (not the binary
    # target); both helpers return 0 for any constant forecast.
    peirce = peirce_skill_score(confusion)
    hss = heidke_skill_score(confusion)
    accuracy = multiclass_accuracy(y_true_int, y_pred_int)
    hard_err = hard_error_rate(y_true_int, y_pred_int)
    mean_cost = _mean_cost_3x3(confusion)
    score = ModelScore(
        name=name,
        n=n,
        accuracy=accuracy,
        peirce=peirce,
        hss=hss,
        hard_error_rate=hard_err,
        mean_cost=mean_cost,
        proba=y_proba,
    )
    if y_proba is not None:
        y_proba = np.asarray(y_proba, dtype=float)
        score.rps = rps_score(y_true_int, y_proba)
        # P(thermal) = P(go) + P(maybe) — the binary forecast probability
        # for Brier and the value curve. Matches the rule baseline's
        # binarisation.
        y_prob_bin = y_proba[:, 0] + y_proba[:, 1]
        score.brier = brier_decomposition(y_bin, y_prob_bin)
        score.value_curve = relative_value_curve(y_bin, y_prob_bin)
        score.value_auc = area_under_value_curve(score.value_curve)
    return score


# --- head-to-head: ML vs rule baseline on the same days --------------------


@dataclass
class HeadToHeadResult:
    ml: ModelScore
    baseline: ModelScore
    mcnemar: McNemarResult | None
    deltas: dict[str, float]
    ml_proba: np.ndarray | None = None
    test: ReplayDataset | None = None

    def as_dict(self) -> dict:
        return {
            "ml": self.ml.as_dict(),
            "baseline": self.baseline.as_dict(),
            "mcnemar": {
                "b": self.mcnemar.b,
                "c": self.mcnemar.c,
                "n_discordant": self.mcnemar.n_discordant,
                "statistic": self.mcnemar.statistic,
                "p_value": self.mcnemar.p_value,
                "exact": self.mcnemar.exact,
                "net": self.mcnemar.net,
            } if self.mcnemar is not None else None,
            "deltas": self.deltas,
        }


def score_head_to_head(
    ml_name: str,
    ml_pred_int: np.ndarray,
    ml_proba: np.ndarray | None,
    baseline_name: str,
    baseline_pred_int: np.ndarray,
    test: ReplayDataset,
    run_mcnemar: bool = True,
) -> HeadToHeadResult:
    """Score the ML model and the rule baseline on the same test days.

    The McNemar paired-significance test is the right one for "did the
    ML model actually beat the rule baseline on this set of N days?"
    (research doc §4.5, Dietterich 1998). Reuses the existing
    `oracle.calibration.mcnemar` so the numbers line up with the rule
    baseline's reports.
    """
    y_true = test.y_int
    ml_score = score_predictions(ml_name, y_true, ml_pred_int, y_proba=ml_proba)
    baseline_score = score_predictions(baseline_name, y_true, baseline_pred_int, y_proba=None)
    mc: McNemarResult | None = None
    if run_mcnemar:
        ml_correct = (ml_pred_int == y_true).tolist()
        base_correct = (baseline_pred_int == y_true).tolist()
        mc = mcnemar(base_correct, ml_correct)
    deltas = {
        "peirce": ml_score.peirce - baseline_score.peirce,
        "hss": ml_score.hss - baseline_score.hss,
        "accuracy": ml_score.accuracy - baseline_score.accuracy,
        "hard_error_rate": ml_score.hard_error_rate - baseline_score.hard_error_rate,
        "mean_cost": ml_score.mean_cost - baseline_score.mean_cost,
    }
    if ml_score.value_auc or baseline_score.value_auc:
        deltas["value_auc"] = ml_score.value_auc - baseline_score.value_auc
    return HeadToHeadResult(
        ml=ml_score,
        baseline=baseline_score,
        mcnemar=mc,
        deltas=deltas,
        ml_proba=ml_proba,
        test=test,
    )


# --- text report ----------------------------------------------------------


def format_text_report(result: HeadToHeadResult) -> str:
    """Plain-text summary suitable for `oracle ml evaluate` stdout."""
    lines: list[str] = []
    n = result.ml.n
    lines.append(
        f"ML ceiling-spike evaluation: {n} test days, "
        f"ML={result.ml.name} vs baseline={result.baseline.name}."
    )
    lines.append("")
    lines.append(
        f"  {'metric':<22s}  {'ML':>9s}  {'baseline':>9s}  {'Δ':>9s}"
    )
    rows: list[tuple[str, float, float, float, str]] = [
        # (name, ml, baseline, delta, precision_spec) — precision_spec is
        # the `.3f` / `.4f` part; sign/width are added per-cell.
        ("Peirce (3-class)",   result.ml.peirce,        result.baseline.peirce,        result.deltas["peirce"],        ".3f"),
        ("HSS (3-class)",      result.ml.hss,           result.baseline.hss,           result.deltas["hss"],           ".3f"),
        ("Accuracy (3-class)", result.ml.accuracy,      result.baseline.accuracy,      result.deltas["accuracy"],      ".4f"),
        ("Hard-error rate",    result.ml.hard_error_rate, result.baseline.hard_error_rate, result.deltas["hard_error_rate"], ".4f"),
        ("Mean cost / day",    result.ml.mean_cost,     result.baseline.mean_cost,     result.deltas["mean_cost"],     ".3f"),
    ]
    if result.ml.proba is not None and result.ml.value_auc:
        rows.append(("Value curve AUC", result.ml.value_auc, result.baseline.value_auc,
                     result.deltas.get("value_auc", 0.0), ".3f"))
    for name, ml, base, delta, prec in rows:
        # ml / base: right-aligned, signed (so +0.107 reads clearly).
        # delta: right-aligned, signed, with explicit `+` so the reader
        # sees the direction of the change at a glance.
        ml_cell = format(ml, f"+9{prec}")
        base_cell = format(base, f"+9{prec}")
        delta_cell = format(delta, f"+9{prec}")
        lines.append(f"  {name:<22s}  {ml_cell}  {base_cell}  {delta_cell}")
    if result.ml.proba is not None:
        lines.append("")
        lines.append("Probabilistic metrics (ML only — the rule baseline is categorical):")
        if result.ml.rps is not None:
            lines.append(f"  RPS (3-class)         {result.ml.rps:.4f}  (0 = perfect, 1 = worst)")
        if result.ml.brier is not None:
            b = result.ml.brier
            lines.append(
                f"  Brier (binary)        {b.bs:.4f}  = REL {b.rel:.4f} − RES {b.res:.4f} + UNC {b.unc:.4f}"
            )
    if result.mcnemar is not None:
        lines.append("")
        mc = result.mcnemar
        method = "exact binomial" if mc.exact else "χ², cont.corr."
        sig = "SIGNIFICANT" if mc.p_value < 0.05 else "not significant"
        lines.append(
            f"McNemar {result.baseline.name} → {result.ml.name}: "
            f"fixed {mc.b}, broke {mc.c} (net {mc.net:+d} of {mc.n_discordant} discordant); "
            f"p={mc.p_value:.3g} [{method}] → {sig} at α=0.05"
        )
    return "\n".join(lines)
