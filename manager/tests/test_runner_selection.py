"""Host occupancy ranking across task types and shared registry reloads."""

import pytest

from app.core.runner_store import RunnerStore
from app.models.models import Runner
from app.services import runner_selection as selection


def worker(identifier, url, **changes):
    """Build a mixed instance, with optional availability or status overrides."""
    return Runner(
        id=identifier,
        url=url,
        task_types=["encoding", "studio", "transcription", "live"],
        token="test-token",
        **changes,
    )


@pytest.fixture
def stable_order(monkeypatch):
    """Keep ties deterministic while testing occupancy preferences."""
    monkeypatch.setattr(selection.random, "shuffle", lambda groups: None)


def test_occupancy_uses_capacity_and_all_task_types(stable_order):
    """One occupied slot out of four is preferred to one out of two."""
    workers = [
        worker("small-free", "http://small.example:8001"),
        worker("small-busy", "http://small.example:8002", availability="busy"),
        worker("large-busy", "http://large.example:8001", availability="busy"),
        *(worker(f"large-{port}", f"http://large.example:{port}") for port in range(8002, 8005)),
    ]
    workers[1].task_types = ["transcription"]
    ranked = selection.rank_runners(iter(workers))
    assert ranked[0].id == "large-busy"
    assert [runner.id for runner in ranked[-2:]] == ["small-free", "small-busy"]
    assert workers[0].id == "small-free"  # Ranking does not reorder the caller's list.
    assert len(ranked) == 6  # A mixed instance contributes only one slot.


def test_offline_instances_do_not_inflate_capacity(stable_order):
    """Offline workers cannot make a half occupied host appear less occupied."""
    workers = [
        worker("a-free", "http://a.example:8001"),
        worker("a-busy", "http://a.example:8002", availability="busy"),
        worker("a-offline", "http://a.example:8003", status="offline"),
        worker("a-offline-2", "http://a.example:8004", status="offline"),
        worker("b-free", "http://b.example:8001"),
        worker("b-free-2", "http://b.example:8002"),
        worker("b-free-3", "http://b.example:8003"),
        worker("b-busy", "http://b.example:8004", availability="busy"),
        worker("offline-only", "http://offline.example", status="offline"),
    ]
    ranked = selection.rank_runners(workers)
    assert ranked[0].id == "b-free"
    assert ranked[-1].id == "offline-only"


@pytest.mark.parametrize(
    "urls",
    [
        ("http://193.51.152.241:8001", "https://193.51.152.241:8002"),
        ("http://HOST.EXAMPLE.:8001", "https://host.example:8002"),
        ("http://[2001:db8::1]:8001", "https://[2001:db8::1]:8002"),
    ],
)
def test_groups_url_hostnames_and_counts_live_reservations(stable_order, urls):
    """Ports, schemes and runner names do not split a host or hide its Live load."""
    workers = [
        worker("arbitrary-name", urls[0]),
        worker("unrelated-name", urls[1], live_task_id="reserved-live"),
        worker("idle", "http://idle.example:8001"),
    ]
    assert selection.rank_runners(workers)[0].id == "idle"


@pytest.mark.parametrize("reverse", [False, True])
def test_randomizes_hosts_and_instances_at_equal_occupancy(monkeypatch, reverse):
    """Tie breaking operates on hosts independently of their instance counts."""
    monkeypatch.setattr(
        selection.random, "shuffle", lambda groups: groups.reverse() if reverse else None
    )
    workers = [
        worker("a1", "http://a.example:8001"),
        worker("a2", "http://a.example:8002"),
        worker("a3", "http://a.example:8003"),
        worker("b1", "http://b.example:8001"),
        worker("b2", "http://b.example:8002"),
    ]
    expected = ["b2", "b1", "a3", "a2", "a1"] if reverse else [r.id for r in workers]
    assert [r.id for r in selection.rank_runners(workers)] == expected


@pytest.mark.parametrize("live", [False, True])
def test_shared_registry_reservations_survive_reload(stable_order, tmp_path, live):
    """Other workers and restarted Managers rank from persisted reservations."""
    path = str(tmp_path / "runners.json")
    first = RunnerStore(shared_enabled=True, state_file=path)
    second = RunnerStore(shared_enabled=True, state_file=path)
    for host in ("a", "b"):
        for port in (8001, 8002):
            first.register(worker(f"{host}-{port}", f"http://{host}.example:{port}"))
    chosen = selection.rank_runners(first.values())[0]
    reserved = first.reserve_live(chosen.id, "live-id") if live else first.try_reserve(chosen.id)
    assert reserved is not None
    assert selection.rank_runners(second.values())[0].id == "b-8001"
    restarted = RunnerStore(shared_enabled=True, state_file=path)
    assert selection.rank_runners(restarted.values())[0].id == "b-8001"


def test_empty_registry():
    """An empty registry has no candidates."""
    assert selection.rank_runners([]) == []
