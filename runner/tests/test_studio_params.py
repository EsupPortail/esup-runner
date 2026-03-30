from app.task_handlers.studio.studio_handler import StudioEncodingHandler


def test_validate_parameters_accepts_studio_encoding_and_tracking_fields():
    handler = StudioEncodingHandler()

    assert (
        handler.validate_parameters(
            {
                "presenter": "piph",
                "force_cpu": False,
                "studio_crf": "23",
                "studio_preset": "medium",
                "studio_audio_bitrate": "128k",
                "studio_allow_nvenc": False,
                "cut": '{"start":"00:00:01","end":"00:00:10"}',
                "rendition": '{"360":{"encode_mp4":true}}',
                "dressing": '{"watermark":"https://example.org/wm.png"}',
                "video_id": "12345",
                "video_slug": "intro-python",
                "video_title": "Introduction a Python",
            }
        )
        is True
    )


def test_validate_parameters_rejects_unknown_studio_fields_with_invalid_list():
    handler = StudioEncodingHandler()

    assert handler.validate_parameters({"unknown": "value", "other": "x"}) is False
    assert handler.get_invalid_parameters({"unknown": "value", "other": "x"}) == [
        "other",
        "unknown",
    ]


def test_build_encoding_args_includes_tracking_and_dressing_flags():
    handler = StudioEncodingHandler()

    params = {
        "cut": '{"start":"00:00:01","end":"00:00:10"}',
        "rendition": '{"360":{"encode_mp4":true}}',
        "dressing": '{"watermark":"https://example.org/wm.png"}',
        "video_id": "12345",
        "video_slug": "intro-python",
        "video_title": "Introduction a Python",
        "presenter": "piph",
        "studio_allow_nvenc": False,
    }

    args = handler._build_encoding_args(
        parameters=params,
        base_dir="/tmp/base",
        input_file="input.mp4",
        work_dir="output",
    )

    assert "--dressing" in args
    assert "--video-id" in args
    assert "--video-slug" in args
    assert "--video-title" in args
    assert args[args.index("--video-id") + 1] == params["video_id"]
    assert args[args.index("--video-slug") + 1] == params["video_slug"]
    assert args[args.index("--video-title") + 1] == params["video_title"]
    assert "--studio_allow_nvenc" not in args
