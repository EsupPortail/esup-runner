import pytest

from app.core.config import _parse_grouped_task_types_spec


def test_grouped_spec_is_detected_and_expanded():
    spec = "[2x(encoding,studio,transcription),1x(encoding,studio),1x(transcription)]"
    expanded = _parse_grouped_task_types_spec(spec)

    assert expanded is not None
    assert len(expanded) == 4

    assert expanded[0] == {"encoding", "studio", "transcription"}
    assert expanded[1] == {"encoding", "studio", "transcription"}
    assert expanded[2] == {"encoding", "studio"}
    assert expanded[3] == {"transcription"}


def test_grouped_spec_allows_whitespace_and_no_brackets():
    spec = " 2x(encoding, studio,transcription) , 1x(encoding,studio) ,1x(transcription) "
    expanded = _parse_grouped_task_types_spec(spec)

    assert expanded is not None
    assert len(expanded) == 4


def test_legacy_spec_returns_none():
    assert _parse_grouped_task_types_spec("encoding,studio,transcription") is None


def test_invalid_grouped_spec_raises():
    with pytest.raises(ValueError):
        _parse_grouped_task_types_spec("[2x(encoding,studio),oops]")
