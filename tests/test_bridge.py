"""The receiver's spool: never block the car, never lose a batch, never reorder.

The bridge runs on a box nobody looks at, and the failure it guards against is
the one no other part of this system covers. The car buffers what it cannot
send and replays it in order, so the car-to-receiver link repairs itself; once
the receiver has the records the car will never send them again, and a batch
dropped between here and the app is gone for good.

zmq is not installed in this environment and is not needed to test any of
that, so it is stubbed at import. Nothing below touches a socket.
"""
import json
import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("zmq", types.SimpleNamespace(
    Context=object, SUB=1, POLLIN=1, SUBSCRIBE=2, Poller=object))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "telemetry"))

import bridge  # noqa: E402


@pytest.fixture(autouse=True)
def _spool_file(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "SPOOL_PATH", str(tmp_path / "spool.jsonl"))
    monkeypatch.setattr(bridge, "SPOOL_DRAIN_MAX", 5)
    monkeypatch.setattr(bridge, "SPOOL_MAX_BATCHES", 100)
    return tmp_path / "spool.jsonl"


def _posts(monkeypatch, outcomes):
    """Stub post() with a scripted sequence of results, recording what it saw."""
    seen, results = [], list(outcomes)

    def fake_post(records):
        seen.append(records)
        return results.pop(0) if results else True

    monkeypatch.setattr(bridge, "post", fake_post)
    return seen


def test_a_batch_the_app_will_not_take_is_kept_not_dropped(monkeypatch, _spool_file):
    seen = _posts(monkeypatch, [False])
    bridge.send([{"vin": "A", "createdAt": "1"}])
    assert seen == [[{"vin": "A", "createdAt": "1"}]]
    assert json.loads(_spool_file.read_text().strip()) == [{"vin": "A", "createdAt": "1"}]


def test_the_spool_goes_out_before_the_live_batch(monkeypatch, _spool_file):
    """Order is the whole point.

    Spooled records are older than live ones and the app builds a trip from a
    stream it reads in order. Send the new batch first and the recovered one
    arrives looking like the odometer running backwards — the exact state the
    trip machine refuses, and it would count them as replays and discard them.
    """
    _posts(monkeypatch, [False])
    bridge.send([{"n": 1}])                       # stuck

    seen = _posts(monkeypatch, [True, True])
    bridge.send([{"n": 2}])                       # app is back
    assert seen == [[{"n": 1}], [{"n": 2}]], "older batch must go first"
    assert not _spool_file.exists()


def test_a_live_batch_queues_behind_a_drain_that_failed(monkeypatch, _spool_file):
    """If the old one still will not go, the new one must not overtake it."""
    _posts(monkeypatch, [False])
    bridge.send([{"n": 1}])

    seen = _posts(monkeypatch, [False])
    bridge.send([{"n": 2}])
    assert seen == [[{"n": 1}]], "the live batch is not even attempted"
    lines = [json.loads(l) for l in _spool_file.read_text().splitlines()]
    assert lines == [[{"n": 1}], [{"n": 2}]]


def test_a_partial_drain_keeps_the_rest_in_order(monkeypatch, _spool_file):
    for i in range(4):
        _posts(monkeypatch, [False])
        bridge.send([{"n": i}])

    # Two get through, the third is refused: the last two must survive intact.
    _posts(monkeypatch, [True, True, False])
    assert bridge.drain() is False
    lines = [json.loads(l) for l in _spool_file.read_text().splitlines()]
    assert lines == [[{"n": 2}], [{"n": 3}]]


def test_a_rejected_batch_is_not_spooled(monkeypatch, _spool_file):
    """post() returns True for a 4xx on purpose.

    A wrong key or a missing endpoint will not fix itself, and spooling it
    would build a backlog that can never drain — and that backlog would then
    block every live batch behind it for ever.
    """
    _posts(monkeypatch, [True])
    bridge.send([{"n": 1}])
    assert not _spool_file.exists()


def test_the_spool_is_bounded_and_keeps_the_newest(monkeypatch, _spool_file):
    monkeypatch.setattr(bridge, "SPOOL_MAX_BATCHES", 3)
    _posts(monkeypatch, [False] * 6)
    for i in range(5):
        bridge.send([{"n": i}])
    lines = [json.loads(l) for l in _spool_file.read_text().splitlines()]
    assert lines == [[{"n": 2}], [{"n": 3}], [{"n": 4}]]


def test_an_unreadable_spool_line_is_discarded_rather_than_blocking(monkeypatch,
                                                                   _spool_file):
    """One corrupt line must not wedge every batch behind it for ever."""
    _spool_file.write_text('not json\n' + json.dumps([{"n": 1}]) + "\n")
    seen = _posts(monkeypatch, [True])
    assert bridge.drain() is True
    assert seen == [[{"n": 1}]]
    assert not _spool_file.exists()


def test_live_only_fields_never_reach_the_database_batch():
    """Pack voltage and current are for the live page: stripped from what the
    bridge batches for storage, and a record of nothing else is dropped."""
    pack = {"vin": "V", "data": [{"key": "PackVoltage", "value": {"doubleValue": 390.0}},
                                 {"key": "PackCurrent", "value": {"doubleValue": 40.0}}]}
    mixed = {"vin": "V", "data": [{"key": "VehicleSpeed", "value": {"doubleValue": 30.0}},
                                  {"key": "PackCurrent", "value": {"doubleValue": 40.0}}]}
    plain = {"vin": "V", "data": [{"key": "Odometer", "value": {"doubleValue": 1.0}}]}
    assert bridge._for_db(pack) is None
    assert [e["key"] for e in bridge._for_db(mixed)["data"]] == ["VehicleSpeed"]
    assert bridge._for_db(plain) is plain
    assert bridge._keys(mixed) & bridge.LIVE_FIELDS == {"VehicleSpeed", "PackCurrent"}
    brake = {"vin": "V", "data": [{"key": "BrakePedal", "value": {"booleanValue": True}}]}
    assert bridge._for_db(brake) is None          # live page only
    assert bridge._keys(brake) & bridge.LIVE_FIELDS == {"BrakePedal"}


def _rec(t, **fields):
    from datetime import datetime, timezone
    return {"vin": "V", "createdAt": datetime.fromtimestamp(t, timezone.utc).isoformat(),
            "data": [{"key": k, "value": {"doubleValue": v}} for k, v in fields.items()]}


def test_the_pacer_holds_the_stored_path_to_the_classic_intervals():
    """Speed every 2 s from the car reaches storage at most every 10 s, and a
    value held back (the car slowing to a stop) is still sent when its
    interval is up — as the car itself would at a 10 s interval."""
    p = bridge.Pacer()
    t0 = 1_800_000_000.0
    assert p.pace(_rec(t0, VehicleSpeed=50.0)) is not None
    assert p.pace(_rec(t0 + 2, VehicleSpeed=30.0)) is None
    assert p.pace(_rec(t0 + 4, VehicleSpeed=0.0, Gear=1.0))["data"] == [
        {"key": "Gear", "value": {"doubleValue": 1.0}}]
    assert p.due(now=0) == []                       # not yet
    late = p.due(now=float("inf"))
    assert [r["data"][0]["value"]["doubleValue"] for r in late] == [0.0]
    assert p.pace(_rec(t0 + 12, VehicleSpeed=10.0)) is not None


def test_a_level_reading_takes_its_energy_reading_with_it():
    p = bridge.Pacer()
    t0 = 1_800_000_000.0
    p.pace(_rec(t0, EnergyRemaining=50.0))
    r = p.pace(_rec(t0 + 3, EnergyRemaining=50.1, BatteryLevel=70.0))
    assert {e["key"] for e in r["data"]} == {"BatteryLevel", "EnergyRemaining"}


def test_replays_pass_unpaced():
    p = bridge.Pacer()
    r = dict(_rec(1_800_000_000.0, VehicleSpeed=5.0), isResend=True)
    assert p.pace(r) is r and p.pace(r) is r


def test_after_a_failed_round_batches_spool_without_waiting(monkeypatch, _spool_file):
    """An app that times out on every batch held the loop for a full round of
    retries per batch, while the car's records piled up unread in ZMQ. After
    one failed round, batches go straight to the spool for a short pause."""
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise bridge.requests.ConnectionError("down")

    monkeypatch.setattr(bridge.requests, "post", boom)
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)
    monkeypatch.setattr(bridge, "APP_URL", "http://app")
    monkeypatch.setattr(bridge, "_down_until", 0.0)
    assert bridge.post([{"vin": "A"}]) is False
    assert len(calls) == bridge.POST_RETRIES
    assert bridge.post([{"vin": "B"}]) is False
    assert len(calls) == bridge.POST_RETRIES      # no new attempt in the pause
    monkeypatch.setattr(bridge, "_down_until", 0.0)
    assert bridge.post([{"vin": "C"}]) is False
    assert len(calls) == 2 * bridge.POST_RETRIES  # tried again once it ended
