"""Aggregator behaviour: only HARD vetos can flip the overall verdict to NO_GO."""
from oracle.engine import _aggregate
from oracle.knowledge.rules import Severity, Signal, Verdict


def _v(signal: Signal, severity: Severity = Severity.NONE, rule: str = "x") -> Verdict:
    return Verdict(rule=rule, signal=signal, reason_en="", reason_de="", severity=severity)


def test_all_go_aggregates_to_go():
    assert _aggregate([_v(Signal.GO), _v(Signal.GO)]) is Signal.GO


def test_any_maybe_with_otherwise_go_is_maybe():
    assert _aggregate([_v(Signal.GO), _v(Signal.MAYBE)]) is Signal.MAYBE


def test_hard_veto_blocks_everything():
    verdicts = [_v(Signal.GO), _v(Signal.NO_GO, Severity.HARD), _v(Signal.GO)]
    assert _aggregate(verdicts) is Signal.NO_GO


def test_soft_veto_alone_does_not_block():
    # A single soft NO_GO with everything else green must NOT be NO_GO.
    # That's the whole point of the re-tier: placeholder thresholds firing
    # one rule should downgrade to MAYBE, not veto the day.
    verdicts = [_v(Signal.GO), _v(Signal.NO_GO, Severity.SOFT), _v(Signal.GO)]
    assert _aggregate(verdicts) is Signal.MAYBE


def test_multiple_soft_vetos_still_only_maybe():
    verdicts = [_v(Signal.NO_GO, Severity.SOFT)] * 5
    assert _aggregate(verdicts) is Signal.MAYBE


def test_hard_wins_over_soft():
    verdicts = [
        _v(Signal.NO_GO, Severity.SOFT, rule="overnight_cooling"),
        _v(Signal.NO_GO, Severity.HARD, rule="foehn_override"),
        _v(Signal.GO),
    ]
    assert _aggregate(verdicts) is Signal.NO_GO


def test_maybe_with_soft_veto_is_still_maybe():
    verdicts = [_v(Signal.MAYBE), _v(Signal.NO_GO, Severity.SOFT), _v(Signal.GO)]
    assert _aggregate(verdicts) is Signal.MAYBE
