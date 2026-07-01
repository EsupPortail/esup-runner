"""VTT writing and readability post-processing helpers.

Rewrites generated cues to improve line breaks and punctuation readability.
Fixes apostrophe and elision wrapping artifacts in French and English text.
Keeps subtitle timing intact while improving rendered display quality.
"""

import importlib.util
import re
from pathlib import Path
from typing import Any, Callable, Dict, Optional

VTT_POSTPROCESS_CUE_BLOCK = tuple[list[str], str]

# Whisper's VTT wrapping can split just before an apostrophe, which creates
# artifacts such as `l` on one line and `'usage` on the next one. We only join
# well-known French/English elision or contraction stems so that we do not
# rewrite arbitrary quoted text.
APOSTROPHE_JOIN_STEMS = frozenset(
    {
        "aren",
        "aujourd",
        "c",
        "couldn",
        "d",
        "didn",
        "doesn",
        "don",
        "hadn",
        "hasn",
        "haven",
        "he",
        "how",
        "i",
        "isn",
        "it",
        "j",
        "jusqu",
        "l",
        "let",
        "lorsqu",
        "m",
        "mustn",
        "n",
        "needn",
        "presqu",
        "puisqu",
        "qu",
        "quelqu",
        "quoiqu",
        "s",
        "she",
        "shouldn",
        "t",
        "that",
        "there",
        "they",
        "wasn",
        "we",
        "weren",
        "what",
        "where",
        "who",
        "won",
        "wouldn",
        "you",
    }
)
APOSTROPHE_JOIN_RE = re.compile(
    r"\b("
    + "|".join(re.escape(stem) for stem in sorted(APOSTROPHE_JOIN_STEMS, key=len, reverse=True))
    + r")\s+'(?=[A-Za-zÀ-ÖØ-öø-ÿ])",
    re.IGNORECASE,
)
TOKEN_EDGE_PUNCT_RE = re.compile(r"^[^A-Za-zÀ-ÖØ-öø-ÿ']+|[^A-Za-zÀ-ÖØ-öø-ÿ'-]+$")
LEADING_APOSTROPHE_TOKEN_RE = re.compile(r"^'[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ-]*")
MAX_APOSTROPHE_CUE_JOIN_GAP_SECONDS = 0.25


def _load_vtt_validation_utils_module():
    """Load validation helpers in both package and file-spec execution modes."""
    try:
        import vtt_validation_utils as module  # type: ignore

        return module
    except ModuleNotFoundError:
        module_path = Path(__file__).resolve().with_name("vtt_validation_utils.py")
        spec = importlib.util.spec_from_file_location(
            "transcription_core_vtt_validation_utils",
            module_path,
        )
        if spec is None or spec.loader is None:
            raise
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def write_vtt_result(
    result: Dict[str, object],
    audio_path: Path,
    out_dir: Path,
    get_writer: Callable[..., Any],
    word_options: Dict[str, object],
    debug: bool,
) -> bool:
    """Write a Whisper transcription result to a VTT file."""
    try:
        writer = get_writer("vtt", str(out_dir))
        filename_stem = Path(audio_path).stem
        if debug:
            print(f"Writing VTT to {out_dir}/{filename_stem}.vtt with options {word_options}")
        writer(result, filename_stem, word_options)  # type: ignore[arg-type]
        return True
    except Exception as exc:
        print(f"Failed to write VTT: {exc}")
        return False


def normalize_vtt_cue_text(text: str) -> str:
    """Normalize cue text spacing before wrapping it again."""
    normalized = " ".join((text or "").split()).strip()
    if not normalized:
        return ""

    # Repair the common `l 'usage` / `s 'est` artifacts introduced by hard wraps.
    return APOSTROPHE_JOIN_RE.sub(r"\1'", normalized)


def split_overlong_vtt_word(word: str, max_line_width: int) -> list[str]:
    """Split one overlong token so every emitted line can respect the width limit."""
    if max_line_width <= 0 or len(word) <= max_line_width:
        return [word]
    return [word[index : index + max_line_width] for index in range(0, len(word), max_line_width)]


def wrap_vtt_cue_text(text: str, max_line_width: int, max_line_count: int) -> list[str]:
    """Wrap cue text into lines that respect the configured width."""
    normalized = normalize_vtt_cue_text(text)
    if not normalized:
        return []

    if max_line_width <= 0:
        return [normalized]

    words = normalized.split()
    wrapped_lines: list[str] = []
    current_line = ""

    for word in words:
        for word_part in split_overlong_vtt_word(word, max_line_width):
            candidate = word_part if not current_line else f"{current_line} {word_part}"
            if current_line and len(candidate) > max_line_width:
                wrapped_lines.append(current_line)
                current_line = word_part
            else:
                current_line = candidate

    if current_line:
        wrapped_lines.append(current_line)

    return wrapped_lines


def chunk_vtt_cue_lines(lines: list[str], max_line_count: int) -> list[list[str]]:
    """Group already wrapped lines into display-safe cue chunks."""
    if not lines:
        return []

    chunk_size = max(1, int(max_line_count))
    return [lines[index : index + chunk_size] for index in range(0, len(lines), chunk_size)]


def split_vtt_cue_text(text: str, max_line_width: int, max_line_count: int) -> list[list[str]]:
    """Split cue text into chunks of at most N lines, each wrapped to the max width."""
    wrapped_lines = wrap_vtt_cue_text(text, max_line_width, max_line_count)
    return chunk_vtt_cue_lines(wrapped_lines, max_line_count)


def parse_vtt_cue_time_range(
    timestamp_line: str,
    *,
    parse_vtt_timestamp_fn: Callable[[str], Optional[float]],
) -> tuple[Optional[float], Optional[float]]:
    """Parse the start/end timestamps from one WebVTT cue header line."""
    if "-->" not in timestamp_line:
        return None, None

    try:
        raw_start, raw_end = timestamp_line.split("-->", 1)
        start_token = raw_start.strip().split()[0]
        end_token = raw_end.strip().split()[0]
    except Exception:
        return None, None

    return parse_vtt_timestamp_fn(start_token), parse_vtt_timestamp_fn(end_token)


def format_vtt_cue_time_range(
    timestamp_line: str,
    start_sec: float,
    end_sec: float,
    *,
    format_vtt_timestamp_fn: Callable[[float], str],
) -> str:
    """Format a cue timestamp line while preserving WebVTT cue settings."""
    settings = ""
    if "-->" in timestamp_line:
        _raw_start, raw_end = timestamp_line.split("-->", 1)
        end_parts = raw_end.strip().split(maxsplit=1)
        if len(end_parts) > 1:
            settings = f" {end_parts[1]}"

    return f"{format_vtt_timestamp_fn(start_sec)} --> {format_vtt_timestamp_fn(end_sec)}{settings}"


def split_vtt_cue_prefixes(
    cue_prefix: list[str],
    chunk_count: int,
    *,
    parse_vtt_timestamp_fn: Optional[Callable[[str], Optional[float]]],
    format_vtt_timestamp_fn: Optional[Callable[[float], str]],
) -> list[list[str]]:
    """Build one cue prefix per text chunk, splitting timing proportionally."""
    if chunk_count <= 1:
        return [cue_prefix]
    if not cue_prefix:
        return [[] for _index in range(chunk_count)]

    timestamp_line = cue_prefix[-1]
    fallback_prefixes = [cue_prefix] + [[timestamp_line] for _index in range(chunk_count - 1)]
    if parse_vtt_timestamp_fn is None or format_vtt_timestamp_fn is None:
        return fallback_prefixes

    start_sec, end_sec = parse_vtt_cue_time_range(
        timestamp_line,
        parse_vtt_timestamp_fn=parse_vtt_timestamp_fn,
    )
    if start_sec is None or end_sec is None or end_sec <= start_sec:
        return fallback_prefixes

    duration_sec = float(end_sec) - float(start_sec)
    split_prefixes: list[list[str]] = []
    for chunk_index in range(chunk_count):
        chunk_start = float(start_sec) + (duration_sec * chunk_index / chunk_count)
        chunk_end = float(start_sec) + (duration_sec * (chunk_index + 1) / chunk_count)
        split_timestamp_line = format_vtt_cue_time_range(
            timestamp_line,
            chunk_start,
            chunk_end,
            format_vtt_timestamp_fn=format_vtt_timestamp_fn,
        )
        if chunk_index == 0:
            split_prefixes.append(cue_prefix[:-1] + [split_timestamp_line])
        else:
            split_prefixes.append([split_timestamp_line])

    return split_prefixes


def cue_gap_allows_apostrophe_transfer(
    previous_timestamp_line: str,
    next_timestamp_line: str,
    *,
    parse_vtt_cue_time_range_fn: Callable[[str], tuple[Optional[float], Optional[float]]],
) -> bool:
    """Return whether two adjacent cues are close enough to repair a split word."""
    previous_start_sec, previous_end_sec = parse_vtt_cue_time_range_fn(previous_timestamp_line)
    next_start_sec, _next_end_sec = parse_vtt_cue_time_range_fn(next_timestamp_line)
    if previous_start_sec is None or previous_end_sec is None or next_start_sec is None:
        return False

    gap_sec = next_start_sec - previous_end_sec
    return 0.0 <= gap_sec <= MAX_APOSTROPHE_CUE_JOIN_GAP_SECONDS


def extract_token_core(token: str) -> str:
    """Return a token stripped from edge punctuation but keeping apostrophes."""
    return TOKEN_EDGE_PUNCT_RE.sub("", (token or "").strip())


def split_leading_token(text: str) -> tuple[str, str]:
    """Split the first token from the remaining normalized text."""
    token, _separator, remainder = (text or "").partition(" ")
    return token, remainder.strip()


def extract_trailing_token_core(
    text: str, *, normalize_vtt_cue_text_fn: Callable[[str], str]
) -> str:
    """Return the normalized last token core from a cue text."""
    normalized = normalize_vtt_cue_text_fn(text)
    if not normalized:
        return ""
    return extract_token_core(normalized.rsplit(" ", 1)[-1])


def repair_cross_cue_apostrophe_split(
    previous_text: str,
    next_text: str,
    *,
    normalize_vtt_cue_text_fn: Callable[[str], str],
    extract_trailing_token_core_fn: Callable[[str], str],
) -> tuple[str, str]:
    """Move a leading apostrophe token back to the previous cue when safe."""
    normalized_previous = normalize_vtt_cue_text_fn(previous_text)
    normalized_next = normalize_vtt_cue_text_fn(next_text)
    if not normalized_previous or not normalized_next:
        return normalized_previous, normalized_next

    previous_last_core = extract_trailing_token_core_fn(normalized_previous)
    if not previous_last_core:
        return normalized_previous, normalized_next

    next_first_token, next_remainder = split_leading_token(normalized_next)
    next_first_core = extract_token_core(next_first_token)
    if LEADING_APOSTROPHE_TOKEN_RE.match(next_first_core) is None:
        return normalized_previous, normalized_next

    # Case 1: previous cue ends with a bare elision stem (`s`, `l`, `we`, `don`).
    if previous_last_core.lower() in APOSTROPHE_JOIN_STEMS:
        merged_previous = normalize_vtt_cue_text_fn(f"{normalized_previous}{next_first_token}")
        return merged_previous, next_remainder

    # Case 2: previous cue already contains the full apostrophe token (`l'institution`,
    # `we're`) and next cue repeats only the apostrophe suffix because of overlap.
    if previous_last_core.lower().endswith(next_first_core.lower()):
        return normalized_previous, next_remainder

    return normalized_previous, normalized_next


def parse_vtt_postprocess_block(block: str) -> str | VTT_POSTPROCESS_CUE_BLOCK:
    """Convert a raw VTT block into either a cue tuple or a passthrough string."""
    if "-->" not in block:
        return block

    block_lines = block.splitlines()
    timestamp_index = next((index for index, line in enumerate(block_lines) if "-->" in line), -1)
    if timestamp_index < 0:
        return block

    cue_prefix = block_lines[: timestamp_index + 1]
    cue_text_lines = [line.strip() for line in block_lines[timestamp_index + 1 :] if line.strip()]
    return (cue_prefix, " ".join(cue_text_lines))


def repair_cross_cue_apostrophe_splits(
    blocks: list[str | VTT_POSTPROCESS_CUE_BLOCK],
    *,
    cue_gap_allows_apostrophe_transfer_fn: Callable[[str, str], bool],
    repair_cross_cue_apostrophe_split_fn: Callable[[str, str], tuple[str, str]],
) -> None:
    """Repair safe apostrophe splits that landed on two adjacent cue blocks."""
    for index in range(len(blocks) - 1):
        previous_block = blocks[index]
        next_block = blocks[index + 1]
        if isinstance(previous_block, str) or isinstance(next_block, str):
            continue

        previous_prefix, previous_text = previous_block
        next_prefix, next_text = next_block
        if not previous_prefix or not next_prefix:
            continue
        if not cue_gap_allows_apostrophe_transfer_fn(previous_prefix[-1], next_prefix[-1]):
            continue

        repaired_previous_text, repaired_next_text = repair_cross_cue_apostrophe_split_fn(
            previous_text,
            next_text,
        )
        blocks[index] = (previous_prefix, repaired_previous_text)
        blocks[index + 1] = (next_prefix, repaired_next_text)


def repair_cross_cue_apostrophe_splits_with_defaults(
    blocks: list[str | VTT_POSTPROCESS_CUE_BLOCK],
    *,
    parse_vtt_timestamp_fn: Callable[[str], Optional[float]],
) -> None:
    """Repair apostrophe splits using the default parser/normalization callbacks."""
    parse_range = lambda line: parse_vtt_cue_time_range(  # noqa: E731
        line,
        parse_vtt_timestamp_fn=parse_vtt_timestamp_fn,
    )
    cue_gap = lambda previous_line, next_line: cue_gap_allows_apostrophe_transfer(  # noqa: E731
        previous_line,
        next_line,
        parse_vtt_cue_time_range_fn=parse_range,
    )
    extract_trailing = lambda text: extract_trailing_token_core(  # noqa: E731
        text,
        normalize_vtt_cue_text_fn=normalize_vtt_cue_text,
    )
    repair_split = lambda previous_text, next_text: repair_cross_cue_apostrophe_split(  # noqa: E731
        previous_text,
        next_text,
        normalize_vtt_cue_text_fn=normalize_vtt_cue_text,
        extract_trailing_token_core_fn=extract_trailing,
    )
    repair_cross_cue_apostrophe_splits(
        blocks,
        cue_gap_allows_apostrophe_transfer_fn=cue_gap,
        repair_cross_cue_apostrophe_split_fn=repair_split,
    )


def render_postprocessed_vtt_blocks(
    blocks: list[str | VTT_POSTPROCESS_CUE_BLOCK],
    *,
    max_line_width: int,
    max_line_count: int,
    wrap_vtt_cue_text_fn: Callable[[str, int, int], list[str]],
    parse_vtt_timestamp_fn: Optional[Callable[[str], Optional[float]]] = None,
    format_vtt_timestamp_fn: Optional[Callable[[float], str]] = None,
) -> list[str]:
    """Render parsed VTT blocks back to strings after readability cleanup."""
    rendered_blocks: list[str] = []
    for parsed_block in blocks:
        if isinstance(parsed_block, str):
            rendered_blocks.append(parsed_block)
            continue

        cue_prefix, cue_text = parsed_block
        wrapped_text_lines = wrap_vtt_cue_text_fn(cue_text, max_line_width, max_line_count)
        if not wrapped_text_lines:
            continue
        text_chunks = chunk_vtt_cue_lines(wrapped_text_lines, max_line_count)
        cue_prefixes = split_vtt_cue_prefixes(
            cue_prefix,
            len(text_chunks),
            parse_vtt_timestamp_fn=parse_vtt_timestamp_fn,
            format_vtt_timestamp_fn=format_vtt_timestamp_fn,
        )
        for split_prefix, text_chunk in zip(cue_prefixes, text_chunks):
            rendered_blocks.append("\n".join(split_prefix + text_chunk))
    return rendered_blocks


def render_postprocessed_vtt_blocks_with_defaults(
    blocks: list[str | VTT_POSTPROCESS_CUE_BLOCK],
    *,
    max_line_width: int,
    max_line_count: int,
) -> list[str]:
    """Render parsed blocks using the default VTT text wrapper."""
    validation_utils = _load_vtt_validation_utils_module()
    return render_postprocessed_vtt_blocks(
        blocks,
        max_line_width=max_line_width,
        max_line_count=max_line_count,
        wrap_vtt_cue_text_fn=wrap_vtt_cue_text,
        parse_vtt_timestamp_fn=validation_utils.parse_vtt_timestamp,
        format_vtt_timestamp_fn=validation_utils.format_vtt_timestamp,
    )


def postprocess_vtt_content(
    content: str,
    *,
    max_line_width: int,
    max_line_count: int,
    parse_vtt_postprocess_block_fn: Callable[[str], str | VTT_POSTPROCESS_CUE_BLOCK],
    repair_cross_cue_apostrophe_splits_fn: Callable[[list[str | VTT_POSTPROCESS_CUE_BLOCK]], None],
    render_postprocessed_vtt_blocks_fn: Callable[..., list[str]],
) -> str:
    """Apply safe readability cleanup to a VTT document."""
    parsed_blocks: list[str | VTT_POSTPROCESS_CUE_BLOCK] = [
        parse_vtt_postprocess_block_fn(block) for block in (content or "").split("\n\n")
    ]
    repair_cross_cue_apostrophe_splits_fn(parsed_blocks)
    processed_blocks = render_postprocessed_vtt_blocks_fn(
        parsed_blocks,
        max_line_width=max_line_width,
        max_line_count=max_line_count,
    )

    return "\n\n".join(processed_blocks).rstrip() + "\n"


def postprocess_vtt_content_with_defaults(
    content: str,
    *,
    max_line_width: int,
    max_line_count: int,
    parse_vtt_timestamp_fn: Callable[[str], Optional[float]],
) -> str:
    """Postprocess VTT using default parse/repair/render callbacks."""
    return postprocess_vtt_content(
        content,
        max_line_width=max_line_width,
        max_line_count=max_line_count,
        parse_vtt_postprocess_block_fn=parse_vtt_postprocess_block,
        repair_cross_cue_apostrophe_splits_fn=lambda blocks: repair_cross_cue_apostrophe_splits_with_defaults(  # noqa: E731
            blocks,
            parse_vtt_timestamp_fn=parse_vtt_timestamp_fn,
        ),
        render_postprocessed_vtt_blocks_fn=render_postprocessed_vtt_blocks_with_defaults,
    )


def postprocess_vtt_file(
    vtt_path: Path,
    *,
    max_line_width: int,
    max_line_count: int,
    debug: bool,
    postprocess_vtt_content_fn: Callable[..., str],
) -> None:
    """Rewrite the generated VTT with small readability-focused fixes."""
    original_content = vtt_path.read_text(encoding="utf-8")
    processed_content = postprocess_vtt_content_fn(
        original_content,
        max_line_width=max_line_width,
        max_line_count=max_line_count,
    )
    if processed_content != original_content:
        vtt_path.write_text(processed_content, encoding="utf-8")
        if debug:
            print(f"Applied readability post-processing to: {vtt_path}")


def postprocess_vtt_file_with_defaults(
    vtt_path: Path,
    *,
    max_line_width: int,
    max_line_count: int,
    debug: bool,
    parse_vtt_timestamp_fn: Optional[Callable[[str], Optional[float]]] = None,
) -> None:
    """Rewrite a VTT file using the default readability cleanup callbacks."""
    parse_timestamp = parse_vtt_timestamp_fn
    if parse_timestamp is None:
        parse_timestamp = _load_vtt_validation_utils_module().parse_vtt_timestamp

    postprocess_vtt_file(
        vtt_path,
        max_line_width=max_line_width,
        max_line_count=max_line_count,
        debug=debug,
        postprocess_vtt_content_fn=lambda content, *, max_line_width, max_line_count: postprocess_vtt_content_with_defaults(
            content,
            max_line_width=max_line_width,
            max_line_count=max_line_count,
            parse_vtt_timestamp_fn=parse_timestamp,
        ),
    )
