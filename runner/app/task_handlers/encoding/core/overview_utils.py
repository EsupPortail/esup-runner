"""Overview (sprite + VTT) generation helpers.

Builds thumbnail sampling plans and sprite-sheet creation commands.
Generates final VTT cue mappings aligned with sprite coordinates.
"""

from __future__ import annotations

import glob
import os
import subprocess


def try_sprite_imagemagick_append(
    *,
    temp_thumb_dir: str,
    num_thumbnails: int,
    sprite_path: str,
) -> tuple[bool, str]:
    """Try to build the overview sprite with ImageMagick as fallback."""
    local_msg = ""
    try:
        png_files = sorted(glob.glob(os.path.join(temp_thumb_dir, "thumb_*.png")))
        if len(png_files) != num_thumbnails or not png_files:
            return (
                False,
                "ImageMagick sprite fallback skipped (missing png thumbs or count mismatch)\n",
            )

        local_msg += "Fallback: ImageMagick +append (png thumbs -> overview.png)\n"
        im_cmd = ["convert", *png_files, "+append", sprite_path]
        im_out = subprocess.run(
            im_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if im_out.returncode == 0 and os.path.exists(sprite_path):
            local_msg += f"Sprite sheet created via ImageMagick: {sprite_path}\n"
            return True, local_msg

        local_msg += f"ImageMagick sprite fallback failed ({im_out.returncode})\n"
        if im_out.stdout:
            local_msg += im_out.stdout
        return False, local_msg
    except FileNotFoundError:
        return False, "convert command not found; cannot use ImageMagick sprite fallback\n"
    except Exception as e:
        return False, f"ImageMagick sprite fallback exception: {e}\n"


def get_overview_max_single_row_thumbnails(
    thumb_width: int,
    thumb_height: int,
    *,
    max_sprite_width: int,
    max_sprite_height: int,
) -> int:
    """Return how many thumbnails can fit in a single-row sprite."""
    if thumb_width <= 0 or thumb_height <= 0:
        raise ValueError("Invalid overview thumbnail dimensions")
    if thumb_width > max_sprite_width or thumb_height > max_sprite_height:
        raise ValueError(
            f"Thumbnail size ({thumb_width}x{thumb_height}) exceeds max sprite "
            f"size ({max_sprite_width}x{max_sprite_height})"
        )
    max_columns = max_sprite_width // thumb_width
    if max_columns < 1:
        raise ValueError(
            f"Thumbnail width {thumb_width} is too large for max sprite width {max_sprite_width}"
        )
    return max_columns


def compute_overview_single_row_plan(
    duration: int,
    requested_interval: int,
    thumb_width: int,
    thumb_height: int,
    *,
    max_sprite_width: int,
    max_sprite_height: int,
) -> tuple[int, int, int, int]:
    """Compute sampling plan for a single-row sprite."""
    interval = max(1, int(requested_interval))
    requested_count = max(1, int(duration / interval))
    max_single_row_count = get_overview_max_single_row_thumbnails(
        thumb_width,
        thumb_height,
        max_sprite_width=max_sprite_width,
        max_sprite_height=max_sprite_height,
    )
    if requested_count <= max_single_row_count:
        return interval, requested_count, requested_count, max_single_row_count

    effective_interval = max(1, (duration + max_single_row_count - 1) // max_single_row_count)
    while (
        effective_interval > 1 and int(duration / (effective_interval - 1)) <= max_single_row_count
    ):
        effective_interval -= 1

    target_count = max(1, int(duration / effective_interval))
    target_count = min(target_count, max_single_row_count)
    return effective_interval, target_count, requested_count, max_single_row_count


def format_overview_thumbnail_plan_msg(
    requested_count: int,
    num_thumbnails: int,
    max_single_row_count: int,
    interval: int,
) -> str:
    """Return a human-readable message for overview thumbnail plan."""
    if requested_count > num_thumbnails:
        return (
            f"Single-row overview requires fewer thumbnails: requested {requested_count}, "
            f"max {max_single_row_count}. Using interval={interval}s "
            f"({num_thumbnails} thumbnails).\n"
        )
    return f"Generating {num_thumbnails} overview thumbnails (1 per {interval}s)\n"


def build_overview_generation_result_msg(
    temp_thumb_dir: str,
    expected_count: int,
) -> tuple[bool, str, int]:
    """Summarize thumbnail generation result from temp directory."""
    generated_files = sorted(glob.glob(os.path.join(temp_thumb_dir, "thumb_*.png")))
    generated_count = len(generated_files)
    if generated_count == 0:
        return False, "Error: FFmpeg reported success but generated no thumbnails\n", 0
    if generated_count != expected_count:
        return (
            True,
            f"Generated {generated_count} thumbnails (requested {expected_count})\n",
            generated_count,
        )
    return True, f"Successfully generated {generated_count} thumbnails\n", generated_count


def generate_overview_thumbnails(
    file: str,
    duration: int,
    output_dir: str,
    *,
    videos_dir: str,
    overview_config: dict,
    run_and_collect_text_fn,
    compute_overview_single_row_plan_fn=compute_overview_single_row_plan,
    format_overview_thumbnail_plan_msg_fn=format_overview_thumbnail_plan_msg,
    build_overview_generation_result_msg_fn=build_overview_generation_result_msg,
) -> tuple[bool, str, int]:
    """Generate individual thumbnails for overview sprite sheet."""
    msg = "--> generate_overview_thumbnails\n"
    if not overview_config.get("enabled", True):
        msg += "Overview generation disabled\n"
        return True, msg, 0

    requested_interval = int(overview_config.get("interval", 1))
    thumb_width = int(overview_config.get("thumbnail_width", 160))
    thumb_height = int(overview_config.get("thumbnail_height", 90))
    max_sprite_width = int(overview_config.get("max_sprite_width", 16384))
    max_sprite_height = int(overview_config.get("max_sprite_height", 16384))

    try:
        interval, num_thumbnails, requested_count, max_single_row_count = (
            compute_overview_single_row_plan_fn(
                duration,
                requested_interval,
                thumb_width,
                thumb_height,
                max_sprite_width=max_sprite_width,
                max_sprite_height=max_sprite_height,
            )
        )
    except ValueError as e:
        msg += f"Error planning overview thumbnails: {e}\n"
        return False, msg, 0

    msg += format_overview_thumbnail_plan_msg_fn(
        requested_count,
        num_thumbnails,
        max_single_row_count,
        interval,
    )

    temp_thumb_dir = os.path.join(output_dir, "overview_temp")
    os.makedirs(temp_thumb_dir, exist_ok=True)
    input_path = os.path.join(videos_dir, file)
    vf = f"fps=1/{interval},scale={thumb_width}:{thumb_height}:flags=lanczos,setsar=1"
    ffmpeg_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        input_path,
        "-vf",
        vf,
        "-frames:v",
        str(num_thumbnails),
        os.path.join(temp_thumb_dir, "thumb_%04d.png"),
    ]
    msg += "cmd: " + " ".join(ffmpeg_cmd) + "\n"

    try:
        rc, out = run_and_collect_text_fn(ffmpeg_cmd)
        if rc == 0:
            ok_count, count_msg, generated_count = build_overview_generation_result_msg_fn(
                temp_thumb_dir,
                num_thumbnails,
            )
            msg += count_msg
            return ok_count, msg, generated_count
        msg += f"Error generating overview thumbnails: {rc}\n"
        if out:
            msg += out
        return False, msg, 0
    except Exception as e:
        msg += f"Exception generating overview thumbnails: {e}\n"
        return False, msg, 0


def create_overview_sprite(
    output_dir: str,
    num_thumbnails: int,
    *,
    overview_config: dict,
    run_shell_bytes_fn,
    try_sprite_imagemagick_append_fn,
    get_overview_max_single_row_thumbnails_fn=get_overview_max_single_row_thumbnails,
) -> tuple[bool, str]:
    """Create sprite sheet from overview thumbnails."""
    msg = "--> create_overview_sprite\n"
    temp_thumb_dir = os.path.join(output_dir, "overview_temp")
    sprite_path = os.path.join(output_dir, "overview.png")

    thumb_width = int(overview_config.get("thumbnail_width", 160))
    thumb_height = int(overview_config.get("thumbnail_height", 90))
    max_sprite_width = int(overview_config.get("max_sprite_width", 16384))
    max_sprite_height = int(overview_config.get("max_sprite_height", 16384))
    try:
        max_single_row_count = get_overview_max_single_row_thumbnails_fn(
            thumb_width,
            thumb_height,
            max_sprite_width=max_sprite_width,
            max_sprite_height=max_sprite_height,
        )
    except ValueError as e:
        msg += f"Error creating sprite sheet: {e}\n"
        return False, msg

    if num_thumbnails > max_single_row_count:
        msg += (
            f"Error creating sprite sheet: {num_thumbnails} thumbnails exceed single-row "
            f"capacity ({max_single_row_count})\n"
        )
        return False, msg

    msg += f"Creating sprite sheet: {num_thumbnails} thumbnails in 1 horizontal row\n"
    ffmpeg_cmd = (
        f"ffmpeg -hide_banner -y "
        f"-pattern_type glob -framerate 1 -i '{temp_thumb_dir}/thumb_*.png' "
        f"-vf 'scale={thumb_width}:{thumb_height}:flags=lanczos,setsar=1,tile={num_thumbnails}x1:margin=0:padding=0' "
        f"-frames:v 1 "
        f"-c:v png "
        f"{sprite_path}"
    )

    try:
        rc0, out0 = run_shell_bytes_fn(ffmpeg_cmd)
        if rc0 != 0:
            msg += f"Error creating sprite sheet: {rc0}\n"
            try:
                msg += out0.decode("utf-8")
            except UnicodeDecodeError:
                pass
            ok_im, im_msg = try_sprite_imagemagick_append_fn(
                temp_thumb_dir=temp_thumb_dir,
                num_thumbnails=num_thumbnails,
                sprite_path=sprite_path,
            )
            msg += im_msg
            if ok_im:
                return True, msg
            return False, msg

        msg += f"Sprite sheet created: {sprite_path}\n"
        return True, msg
    except Exception as e:
        msg += f"Exception creating sprite sheet: {e}\n"
        return False, msg
    finally:
        try:
            import shutil

            shutil.rmtree(temp_thumb_dir)
            msg += "Cleaned up temporary thumbnails\n"
        except Exception as e:
            msg += f"Warning: Could not clean up temp dir: {e}\n"


def format_vtt_timestamp(seconds: int) -> str:
    """Format seconds as WebVTT timestamp."""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.000"


def generate_overview_vtt(
    output_dir: str,
    duration: int,
    num_thumbnails: int,
    *,
    overview_config: dict,
    format_vtt_timestamp_fn,
) -> tuple[bool, str]:
    """Generate WebVTT mapping for overview sprite."""
    msg = "--> generate_overview_vtt\n"
    if num_thumbnails <= 0:
        msg += "Error creating VTT file: no thumbnails available\n"
        return False, msg

    vtt_path = os.path.join(output_dir, "overview.vtt")
    thumb_width = int(overview_config.get("thumbnail_width", 160))
    thumb_height = int(overview_config.get("thumbnail_height", 90))

    try:
        with open(vtt_path, "w") as vtt_file:
            vtt_file.write("WEBVTT\n\n")
            for i in range(num_thumbnails):
                start_time = int(i * duration / num_thumbnails)
                end_time = int(min(duration, (i + 1) * duration / num_thumbnails))
                if end_time <= start_time:
                    end_time = min(duration, start_time + 1)
                x = i * thumb_width
                y = 0
                start_str = format_vtt_timestamp_fn(start_time)
                end_str = format_vtt_timestamp_fn(end_time)
                vtt_file.write(f"{start_str} --> {end_str}\n")
                vtt_file.write(f"overview.png#xywh={x},{y},{thumb_width},{thumb_height}\n\n")

        msg += f"VTT file created: {vtt_path}\n"
        return True, msg
    except Exception as e:
        msg += f"Error creating VTT file: {e}\n"
        return False, msg


def generate_overview(
    file: str,
    duration: int,
    *,
    videos_output_dir: str,
    overview_config: dict,
    generate_overview_thumbnails_fn,
    create_overview_sprite_fn,
    generate_overview_vtt_fn,
) -> tuple[bool, str]:
    """Generate complete overview (thumbnails + sprite + VTT)."""
    msg = "--> generate_overview\n"
    if not overview_config.get("enabled", True):
        msg += "Overview generation is disabled\n"
        return True, msg
    if duration < 1:
        msg += "Video too short for overview generation\n"
        return True, msg

    success, thumb_msg, num_thumbnails = generate_overview_thumbnails_fn(
        file,
        duration,
        videos_output_dir,
    )
    msg += thumb_msg
    if not success or num_thumbnails == 0:
        msg += "Failed to generate overview thumbnails\n"
        return False, msg

    success, sprite_msg = create_overview_sprite_fn(videos_output_dir, num_thumbnails)
    msg += sprite_msg
    if not success:
        msg += "Failed to create sprite sheet\n"
        return False, msg

    success, vtt_msg = generate_overview_vtt_fn(videos_output_dir, duration, num_thumbnails)
    msg += vtt_msg
    if not success:
        msg += "Failed to generate VTT file\n"
        return False, msg

    msg += "Overview generation complete\n"
    return True, msg
