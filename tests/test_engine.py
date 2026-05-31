"""Aggregator behaviour: consensus semantics — HARD vetos always block, SOFT
vetos downgrade only once two or more pile up, MAYBE emissions are advisory."""
from oracle.engine import aggregate
from oracle.knowledge.rules import Severity, Signal, Verdict


def _v(signal: Signal, severity: Severity = Severity.NONE, rule: str = "x") -> Verdict:
    return Verdict(rule=rule, signal=signal, reason_en="", reason_de="", severity=severity)


def test_all_go_aggregates_to_go():
    assert aggregate([_v(Signal.GO), _v(Signal.GO)]) is Signal.GO


def test_hard_veto_blocks_everything():
    verdicts = [_v(Signal.GO), _v(Signal.NO_GO, Severity.HARD), _v(Signal.GO)]
    assert aggregate(verdicts) is Signal.NO_GO


def test_hard_wins_over_soft():
    verdicts = [
        _v(Signal.NO_GO, Severity.SOFT, rule="overnight_cooling"),
        _v(Signal.NO_GO, Severity.HARD, rule="foehn_override"),
        _v(Signal.GO),
    ]
    assert aggregate(verdicts) is Signal.NO_GO


def test_single_soft_veto_does_not_downgrade():
    # The whole point of consensus aggregation: one over-sensitive rule
    # shouldn't override eight rules that say GO.
    verdicts = [_v(Signal.GO)] * 8 + [_v(Signal.NO_GO, Severity.SOFT)]
    assert aggregate(verdicts) is Signal.GO


def test_two_soft_vetos_downgrade_to_maybe():
    # Two converging negative signals = real concern, downgrade.
    verdicts = [_v(Signal.GO)] * 7 + [_v(Signal.NO_GO, Severity.SOFT)] * 2
    assert aggregate(verdicts) is Signal.MAYBE


def test_many_soft_vetos_still_only_maybe():
    # Soft alone never reaches NO_GO regardless of count.
    verdicts = [_v(Signal.NO_GO, Severity.SOFT)] * 5
    assert aggregate(verdicts) is Signal.MAYBE


def test_maybe_emissions_alone_do_not_downgrade():
    # Rules emitting MAYBE are advisory; absent any SOFT NO_GO they don't
    # block consensus GO. (A rule that's genuinely confident in "no" should
    # emit NO_GO, not MAYBE.)
    verdicts = [_v(Signal.GO)] * 6 + [_v(Signal.MAYBE)] * 3
    assert aggregate(verdicts) is Signal.GO


def test_one_soft_plus_maybes_is_still_go():
    # Single soft veto plus any number of MAYBEs is below the downgrade
    # threshold — counts soft NO_GOs only.
    verdicts = [_v(Signal.GO)] * 5 + [_v(Signal.NO_GO, Severity.SOFT)] + [_v(Signal.MAYBE)] * 3
    assert aggregate(verdicts) is Signal.GO
