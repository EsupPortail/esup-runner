# Security Policy

## Supported Versions

If you want most up to date secured version of Esup-Runner, we encourage you to upgrade to the last release.

## Reporting a Vulnerability

As soon as you found a vulnerability issue in Esup-Runner, let us know by posting a github issue.

## Runner Media Codec Denylist

Runner deployments can configure `MEDIA_CODEC_DENYLIST` in `runner/.env`.
It defaults to `magicyuv` so downloaded media matching known MagicYUV binary
signatures is rejected before FFmpeg, ffprobe, or Whisper process the file.
This mitigates exposure to CVE-2026-8461 / PixelSmash while the underlying
FFmpeg runtime is being updated or rebuilt.
