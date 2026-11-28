"""Tests for simprod/faults.py: seeded storms, their fixes and wrong fixes, and injection."""

from dataclasses import asdict

from simprod.faults import BACKOFF_OFF, BACKOFF_ON, FAULTS_VERSION, KINDS, Storm, cap, cap_rps, inject, main, storms


def test_storms_are_seeded_cycle_the_kinds_and_are_in_range() -> None:
    a = storms(21, 200, seed=7)
    assert a == storms(21, 200, seed=7) and a != storms(21, 200, seed=8)
    assert [s.kind for s in a[:4]] == ["retry_storm", "hot_shard", "compound", "retry_storm"]
    assert {k: sum(s.kind == k for s in a) for k in KINDS} == {k: 7 for k in KINDS}
    service_ms = 2000 / 200
    for s in a:
        assert s.decoy.startswith("acct-") and s.decoy != s.key
        if s.kind == "retry_storm":
            assert 2 * service_ms <= s.extra_ms <= 5 * service_ms and 5 <= s.trigger_s <= 15 and s.key == ""
        else:
            assert 0.5 * 200 <= s.rps <= 0.75 * 200 and s.key.startswith("acct-")
    assert len({s.id for s in a}) == 21


def test_only_a_hot_shard_meets_a_gateway_that_backs_off() -> None:
    retry, hot, compound = storms(3, 200, seed=1)
    assert (retry.gateway_backoff, hot.gateway_backoff, compound.gateway_backoff) == (False, True, False)


def test_each_kind_has_one_fix_set_a_wrong_set_and_their_undos() -> None:
    retry, hot, compound = storms(3, 200, seed=1)
    c = cap_rps(200)
    assert c == 10.0
    assert retry.fix(200) == [BACKOFF_ON] and retry.undo_of_fix() == [BACKOFF_OFF]
    assert hot.fix(200) == [cap(hot.key, c)] and hot.undo_of_fix() == [cap(hot.key, None)]
    assert compound.fix(200) == [cap(compound.key, c), BACKOFF_ON]
    assert compound.undo_of_fix() == [BACKOFF_OFF, cap(compound.key, None)]
    # Every wrong fix caps the decoy, never the runaway; compound's adds the backoff it also needs.
    assert retry.wrong(200) == [cap(retry.decoy, c)] and hot.wrong(200) == [cap(hot.decoy, c)]
    assert compound.wrong(200) == [BACKOFF_ON, cap(compound.decoy, c)]
    for s in (retry, hot, compound):
        undo = s.undo_of_wrong()
        assert len(undo) == len(s.wrong(200)) and cap(s.decoy, None) in undo


class Fake:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def call(self, method, path, body, timeout_s, headers=None):
        self.calls.append((method, path, body))
        return 200, {"active": True}


def test_inject_reaches_the_right_sealed_service() -> None:
    retry, hot, compound = storms(3, 200, seed=1)
    ledger, customers = Fake(), Fake()
    for s in (retry, hot, compound):
        inject(s, ledger, customers)  # type: ignore[arg-type]
    assert ledger.calls[0][2]["kind"] == "slowdown" and ledger.calls[0][2]["seconds"] == retry.trigger_s
    assert len(ledger.calls) == 1
    assert customers.calls[0][2] == {"kind": "runaway", "key": hot.key, "rps": hot.rps, "seconds": 3600.0}
    assert customers.calls[1][2]["key"] == compound.key


def test_a_storm_round_trips_through_json(capsys, monkeypatch) -> None:
    import json

    from simprod import faults

    ledger, customers = Fake(), Fake()
    made = iter([ledger, customers])
    monkeypatch.setattr(faults, "Upstream", lambda host, port: next(made))
    storm = Storm("s1-00", "retry_storm", 5, "acct-0001", extra_ms=30.0, trigger_s=8.0)
    assert main(["inject", json.dumps(asdict(storm))]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["injected"] == asdict(storm) and out["faults_version"] == FAULTS_VERSION
