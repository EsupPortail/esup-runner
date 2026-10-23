"""Shared host occupancy ranking for Live and file-based task dispatch."""

import random
from collections.abc import Iterable
from urllib.parse import urlsplit

from app.models.models import Runner


def rank_runners(runners: Iterable[Runner]) -> list[Runner]:
    """Rank hosts by occupied/online instances, randomizing ties and instances.

    Count all task types and each mixed instance once. Group by the advertised
    URL hostname, ignoring ports. Offline instances do not contribute capacity;
    hosts without online instances rank last. Callers retain their eligibility
    checks and atomic reservations because this snapshot is only a preference.
    """
    by_host: dict[str, list[Runner]] = {}
    for runner in runners:
        host = (urlsplit(runner.url).hostname or runner.url).rstrip(".")
        by_host.setdefault(host, []).append(runner)

    def occupancy(group: list[Runner]) -> float:
        """Return the share of online instances occupied by any task type."""
        online = [runner for runner in group if runner.status == "online"]
        if not online:
            return 1.0
        return sum(
            runner.availability != "available" or bool(runner.live_task_id) for runner in online
        ) / len(online)

    groups = list(by_host.values())
    random.shuffle(groups)
    groups.sort(key=occupancy)
    ranked = []
    for group in groups:
        random.shuffle(group)
        ranked.extend(group)
    return ranked
