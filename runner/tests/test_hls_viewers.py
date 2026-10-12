"""Estimate active clients for Runner-served HLS sessions."""

from app.services import hls_viewers


def test_viewer_activity_expires_and_can_be_cleared(monkeypatch):
    current_time = [100.0]
    monkeypatch.setattr(hls_viewers.time, "monotonic", lambda: current_time[0])
    live_id = "live-test"

    hls_viewers.record_request(live_id, "client-a")
    hls_viewers.record_request(live_id, "client-b")
    assert hls_viewers.count(live_id) == 2

    current_time[0] = 115.1
    assert hls_viewers.count(live_id) == 0
    hls_viewers.record_request(live_id, "client-a")
    hls_viewers.clear(live_id)
    assert hls_viewers.count(live_id) == 0
