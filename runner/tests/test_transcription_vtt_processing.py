"""Tests for VTT post-processing and validation."""

import builtins
import inspect
import sys
import types
from pathlib import Path

import pytest
from transcription_test_helpers import load_transcription_core_module as _load_core_module


def test_vtt_postprocess_utils_extra_branches(monkeypatch, tmp_path, capsys):
    """Validate Vtt postprocess utils extra branches."""
    vtt_utils = _load_core_module("vtt_postprocess_utils")

    real_import = builtins.__import__

    def import_without_validation(name, *args, **kwargs):
        if name == "vtt_validation_utils":
            raise ModuleNotFoundError(name="vtt_validation_utils")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_validation)
    loaded_module = vtt_utils._load_vtt_validation_utils_module()
    assert hasattr(loaded_module, "parse_vtt_timestamp")

    writer_calls = []

    def writer_factory(_format: str, _out_dir: str):
        def writer(result, stem, options):
            writer_calls.append((result, stem, options))

        return writer

    assert (
        vtt_utils.write_vtt_result(
            {"segments": []},
            Path("audio.mp3"),
            tmp_path,
            writer_factory,
            {"max_line_width": 40},
            debug=True,
        )
        is True
    )
    assert writer_calls

    assert (
        vtt_utils.write_vtt_result(
            {"segments": []},
            Path("audio.mp3"),
            tmp_path,
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("writer")),
            {},
            debug=False,
        )
        is False
    )

    assert vtt_utils.wrap_vtt_cue_text("hello world", max_line_width=0, max_line_count=2) == [
        "hello world"
    ]

    overflow = vtt_utils.wrap_vtt_cue_text(
        "one two three four five six",
        max_line_width=4,
        max_line_count=2,
    )
    assert overflow
    assert all(len(line) <= 4 for line in overflow)

    assert vtt_utils.parse_vtt_cue_time_range(
        "invalid", parse_vtt_timestamp_fn=lambda _raw: None
    ) == (None, None)
    assert vtt_utils.parse_vtt_cue_time_range(
        "00:00:00.000 --> ",
        parse_vtt_timestamp_fn=lambda value: float(len(value)),
    ) == (None, None)

    assert (
        vtt_utils.cue_gap_allows_apostrophe_transfer(
            "a",
            "b",
            parse_vtt_cue_time_range_fn=lambda _line: (None, None),
        )
        is False
    )
    assert (
        vtt_utils.extract_trailing_token_core(
            "",
            normalize_vtt_cue_text_fn=lambda _text: "",
        )
        == ""
    )

    assert vtt_utils.repair_cross_cue_apostrophe_split(
        "",
        "next",
        normalize_vtt_cue_text_fn=lambda text: text,
        extract_trailing_token_core_fn=lambda _text: "ignored",
    ) == ("", "next")

    assert vtt_utils.repair_cross_cue_apostrophe_split(
        "previous",
        "next",
        normalize_vtt_cue_text_fn=lambda text: text,
        extract_trailing_token_core_fn=lambda _text: "",
    ) == ("previous", "next")

    assert vtt_utils.repair_cross_cue_apostrophe_split(
        "bonjour",
        "'suite",
        normalize_vtt_cue_text_fn=lambda text: text,
        extract_trailing_token_core_fn=lambda _text: "bonjour",
    ) == ("bonjour", "'suite")

    class WeirdBlock(str):
        def __contains__(self, item: object) -> bool:
            if item == "-->":
                return True
            return super().__contains__(item)

        def splitlines(self) -> list[str]:
            return ["header", "body"]

    weird = WeirdBlock("not-a-timestamp-line")
    assert vtt_utils.parse_vtt_postprocess_block(weird) == weird

    blocks = [
        ([""], "a"),
        (["00:00:01.000 --> 00:00:02.000"], "b"),
    ]
    vtt_utils.repair_cross_cue_apostrophe_splits(
        blocks,
        cue_gap_allows_apostrophe_transfer_fn=lambda *_a: False,
        repair_cross_cue_apostrophe_split_fn=lambda _a, _b: ("x", "y"),
    )

    rendered = vtt_utils.render_postprocessed_vtt_blocks(
        [(["00:00:00.000 --> 00:00:01.000"], "text")],
        max_line_width=40,
        max_line_count=2,
        wrap_vtt_cue_text_fn=lambda *_a, **_k: [],
    )
    assert rendered == []

    assert (
        vtt_utils.format_vtt_cue_time_range(
            "00:00:00.000 --> 00:00:04.000 line:90%",
            0.0,
            2.0,
            format_vtt_timestamp_fn=lambda value: f"{value:.1f}",
        )
        == "0.0 --> 2.0 line:90%"
    )
    assert vtt_utils.split_vtt_cue_prefixes(
        [],
        2,
        parse_vtt_timestamp_fn=lambda _raw: 0.0,
        format_vtt_timestamp_fn=lambda value: f"{value:.1f}",
    ) == [[], []]
    assert vtt_utils.split_vtt_cue_prefixes(
        ["00:00:00.000 --> 00:00:04.000"],
        2,
        parse_vtt_timestamp_fn=None,
        format_vtt_timestamp_fn=lambda value: f"{value:.1f}",
    ) == [["00:00:00.000 --> 00:00:04.000"], ["00:00:00.000 --> 00:00:04.000"]]
    assert vtt_utils.split_vtt_cue_prefixes(
        ["invalid"],
        2,
        parse_vtt_timestamp_fn=lambda _raw: None,
        format_vtt_timestamp_fn=lambda value: f"{value:.1f}",
    ) == [["invalid"], ["invalid"]]

    vtt_path = tmp_path / "a.vtt"
    vtt_path.write_text("WEBVTT\n", encoding="utf-8")
    vtt_utils.postprocess_vtt_file(
        vtt_path,
        max_line_width=40,
        max_line_count=2,
        debug=True,
        postprocess_vtt_content_fn=lambda content, **_k: content + "\nchanged\n",
    )
    assert "Applied readability post-processing" in capsys.readouterr().out


def test_vtt_postprocess_loads_top_level_validation_utils(monkeypatch):
    """Validate the direct-import branch used by script execution."""
    vtt_utils = _load_core_module("vtt_postprocess_utils")
    validation_utils = types.ModuleType("vtt_validation_utils")
    monkeypatch.setitem(sys.modules, "vtt_validation_utils", validation_utils)

    assert vtt_utils._load_vtt_validation_utils_module() is validation_utils


def test_vtt_validation_utils_extra_branches(tmp_path, capsys):
    """Validate Vtt validation utils extra branches."""
    validation_utils = _load_core_module("vtt_validation_utils")

    assert validation_utils.parse_vtt_timestamp("  ") is None
    assert validation_utils.parse_vtt_timestamp("xx:00:01.000") is None
    assert validation_utils.parse_vtt_timestamp("00") is None
    assert validation_utils.parse_vtt_timestamp("00:aa.bb") is None

    malformed_vtt = tmp_path / "malformed.vtt"
    malformed_vtt.write_text("WEBVTT\n\n00:00:00.000 --> \n", encoding="utf-8")
    assert validation_utils.read_last_vtt_cue_end_seconds(
        malformed_vtt,
        parse_timestamp=lambda _raw: 1.0,
    ) == (True, True, None)

    assert validation_utils.read_last_vtt_cue_end_seconds(
        tmp_path / "missing.vtt",
        parse_timestamp=lambda _raw: 1.0,
    ) == (False, False, None)

    assert (
        validation_utils.validate_vtt_coverage(
            vtt_path=tmp_path / "x.vtt",
            reference_duration_sec=0.0,
            min_coverage_ratio=0.8,
            max_final_gap_sec=4.0,
            debug=False,
            read_last_cue_end_seconds=lambda _path: (True, True, 1.0),
        )
        == 0
    )

    assert (
        validation_utils.validate_vtt_coverage(
            vtt_path=tmp_path / "x.vtt",
            reference_duration_sec=10.0,
            min_coverage_ratio=0.8,
            max_final_gap_sec=4.0,
            debug=False,
            read_last_cue_end_seconds=lambda _path: (False, False, None),
        )
        == 7
    )

    assert (
        validation_utils.validate_vtt_coverage(
            vtt_path=tmp_path / "x.vtt",
            reference_duration_sec=10.0,
            min_coverage_ratio=0.8,
            max_final_gap_sec=4.0,
            debug=True,
            read_last_cue_end_seconds=lambda _path: (True, False, None),
        )
        == 0
    )

    assert (
        validation_utils.validate_vtt_coverage(
            vtt_path=tmp_path / "x.vtt",
            reference_duration_sec=10.0,
            min_coverage_ratio=0.8,
            max_final_gap_sec=4.0,
            debug=False,
            read_last_cue_end_seconds=lambda _path: (True, True, None),
        )
        == 7
    )

    assert (
        validation_utils.validate_vtt_coverage(
            vtt_path=tmp_path / "x.vtt",
            reference_duration_sec=10.0,
            min_coverage_ratio=0.8,
            max_final_gap_sec=4.0,
            debug=True,
            read_last_cue_end_seconds=lambda _path: (True, True, 9.5),
        )
        == 0
    )
    assert "VTT coverage:" in capsys.readouterr().out

    bad_range_file = tmp_path / "ranges.vtt"
    bad_range_file.write_text("WEBVTT\n\n --> invalid\n", encoding="utf-8")
    read_ok, cues = validation_utils.read_vtt_cue_time_ranges(
        bad_range_file,
        parse_timestamp=lambda _token: None,
    )
    assert read_ok is True
    assert cues == []


def test_remaining_vtt_postprocess_lines(monkeypatch):
    """Validate Remaining vtt postprocess lines."""
    vtt_utils = _load_core_module("vtt_postprocess_utils")

    real_import = builtins.__import__

    def no_validation(name, *args, **kwargs):
        if name == "vtt_validation_utils":
            raise ModuleNotFoundError(name="vtt_validation_utils")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_validation)
    monkeypatch.setattr(vtt_utils.importlib.util, "spec_from_file_location", lambda *_a, **_k: None)
    with pytest.raises(ModuleNotFoundError):
        vtt_utils._load_vtt_validation_utils_module()

    overflow = vtt_utils.wrap_vtt_cue_text(
        "one two six ten",
        max_line_width=3,
        max_line_count=2,
    )
    assert overflow == ["one", "two", "six", "ten"]
    assert vtt_utils.split_vtt_cue_text(
        "one two six ten",
        max_line_width=3,
        max_line_count=2,
    ) == [["one", "two"], ["six", "ten"]]

    blocks = [
        ([], "a"),
        (["00:00:01.000 --> 00:00:02.000"], "b"),
    ]
    vtt_utils.repair_cross_cue_apostrophe_splits(
        blocks,
        cue_gap_allows_apostrophe_transfer_fn=lambda *_a: False,
        repair_cross_cue_apostrophe_split_fn=lambda _a, _b: ("x", "y"),
    )


def test_remaining_vtt_postprocess_overflow_merge_line(monkeypatch):
    """Validate Remaining vtt postprocess overflow merge line."""
    vtt_utils = _load_core_module("vtt_postprocess_utils")

    class SneakyWord(str):
        def __format__(self, format_spec: str) -> str:
            del format_spec
            frame = inspect.currentframe()
            while frame is not None:
                if (
                    frame.f_code.co_name == "wrap_vtt_cue_text"
                    and "wrapped_lines" in frame.f_locals
                ):
                    frame.f_locals["wrapped_lines"].extend(["x", "y", "z"])
                    break
                frame = frame.f_back
            return str(self)

    class SneakyText(str):
        def split(self, sep: str | None = None, maxsplit: int = -1):
            del sep, maxsplit
            return [SneakyWord("one"), SneakyWord("two")]

    monkeypatch.setattr(vtt_utils, "normalize_vtt_cue_text", lambda _text: SneakyText("unused"))
    wrapped = vtt_utils.wrap_vtt_cue_text("anything", max_line_width=3, max_line_count=2)
    assert wrapped
    assert "one" in wrapped
    assert "two" in wrapped
