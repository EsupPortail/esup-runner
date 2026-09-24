"""Remote mediapackages cannot select local files as media tracks."""

from types import SimpleNamespace
from xml.sax.saxutils import escape

import pytest

from app.task_handlers.studio.core import download_runtime_utils, metadata_runtime_utils


def _load_track(source, kind="presentation"):
    xml = (
        '<mediapackage xmlns="http://mediapackage.opencastproject.org">'
        f'<media><track type="{kind}/source"><url>{escape(source)}</url></track></media>'
        "</mediapackage>"
    )
    return metadata_runtime_utils.load_mediapackage_and_layout(
        SimpleNamespace(xml_url="https://example.org/package.xml", presenter=None),
        fetch_text_fn=lambda _: xml,
    )


@pytest.mark.parametrize("kind", ["presentation", "presenter"])
@pytest.mark.parametrize(
    "source",
    [
        "/srv/private.mp4",
        "../private.mp4",
        "local.mp4",
        "file:///srv/private.mp4",
        "//server/video.mp4",
        "ftp://example.org/video.mp4",
        "http:///video.mp4",
    ],
)
def test_local_or_unsupported_xml_track_is_rejected_before_materialization(source, kind):
    with pytest.raises(ValueError, match=r"HTTP\(S\)"):
        _load_track(source, kind)


def test_xml_symlink_is_rejected_but_internal_local_media_stays_supported(tmp_path):
    media = tmp_path / "private.mp4"
    media.write_bytes(b"safe-test-media")
    link = tmp_path / "linked.mp4"
    link.symlink_to(media)
    with pytest.raises(ValueError, match=r"HTTP\(S\)"):
        _load_track(str(link))
    assert download_runtime_utils.materialize_source(str(media), str(tmp_path), "input") == str(
        media
    )


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("kind", ["presentation", "presenter"])
def test_http_xml_tracks_remain_supported(scheme, kind):
    source = f"{scheme}://example.org/video.mp4"
    pres, pers, layout, smil = _load_track(source, kind)
    assert (pres, pers) == ((source, None) if kind == "presentation" else (None, source))
    assert layout == "mid"
    assert smil is None
