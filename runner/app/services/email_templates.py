"""Reusable HTML email rendering helpers."""

from __future__ import annotations

from collections.abc import Sequence
from email.message import EmailMessage
from html import escape
from pathlib import Path
from typing import cast

DetailRows = Sequence[tuple[str, str | None]]

LOGO_CID = "esup-runner-logo"
DEFAULT_LOGO_PATH = Path(__file__).resolve().parents[1] / "web" / "static" / "logo.png"

TONE_STYLES = {
    "danger": {
        "accent": "#dc3545",
        "badge_bg": "#fdecec",
        "badge_text": "#842029",
        "panel_bg": "#fff5f5",
    },
    "warning": {
        "accent": "#fd7e14",
        "badge_bg": "#fff4e5",
        "badge_text": "#7a3f00",
        "panel_bg": "#fff8ef",
    },
    "success": {
        "accent": "#198754",
        "badge_bg": "#e8f6ef",
        "badge_text": "#0f5132",
        "panel_bg": "#f2fbf6",
    },
    "info": {
        "accent": "#0d6bf4",
        "badge_bg": "#eaf3ff",
        "badge_text": "#084298",
        "panel_bg": "#f5f9ff",
    },
}


def _display(value: str | None, fallback: str = "(none)") -> str:
    """Return a stripped display value, or the fallback when it is blank."""
    cleaned = (value or "").strip()
    return cleaned or fallback


def _tone_style(tone: str) -> dict[str, str]:
    """Return the color style for an email tone."""
    return TONE_STYLES.get(tone, TONE_STYLES["info"])


def _is_http_url(value: str | None) -> bool:
    """Return True when a value is an HTTP(S) URL safe for email links."""
    return bool(value and value.startswith(("http://", "https://")))


def _render_logo(logo_cid: str | None, product_name: str) -> str:
    """Render either an inline logo image or a text product fallback."""
    if not logo_cid:
        return (
            '<div style="font-size:22px;font-weight:700;color:#1f2937;line-height:1.2;">'
            f"{escape(product_name)}</div>"
        )

    return (
        f'<img src="cid:{logo_cid}" width="190" alt="{escape(product_name)}" '
        'style="display:block;width:190px;max-width:100%;height:auto;border:0;outline:none;">'
    )


def _render_detail_rows(details: DetailRows) -> str:
    """Render key/value detail rows for the HTML email summary table."""
    rows: list[str] = []
    for label, value in details:
        rows.append(
            "<tr>"
            '<td style="padding:10px 12px;border-bottom:1px solid #e9ecef;'
            'font-size:13px;color:#6c747c;width:34%;vertical-align:top;">'
            f"{escape(label)}"
            "</td>"
            '<td style="padding:10px 12px;border-bottom:1px solid #e9ecef;'
            "font-size:14px;color:#212529;font-weight:600;vertical-align:top;"
            'word-break:break-word;">'
            f"{escape(_display(value))}"
            "</td>"
            "</tr>"
        )
    return "".join(rows)


def _render_block(title: str, body: str | None, tone: str) -> str:
    """Render an optional preformatted content block."""
    if body is None:
        return ""

    style = _tone_style(tone)
    return (
        '<tr><td style="padding:0 28px 24px 28px;background:#ffffff;">'
        f'<div style="border-left:5px solid {style["accent"]};'
        f'background:{style["panel_bg"]};border-radius:6px;padding:16px 18px;">'
        '<div style="font-size:13px;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0;color:{style["badge_text"]};margin-bottom:10px;">'
        f"{escape(title)}</div>"
        '<pre style="margin:0;font-family:Menlo,Consolas,Monaco,monospace;'
        "font-size:13px;line-height:1.5;color:#212529;white-space:pre-wrap;"
        'word-break:break-word;">'
        f"{escape(_display(body, '(no details)'))}</pre>"
        "</div>"
        "</td></tr>"
    )


def render_html_email(
    *,
    product_name: str,
    eyebrow: str,
    title: str,
    summary: str,
    status_label: str,
    tone: str,
    details: DetailRows,
    primary_block_title: str,
    primary_block_body: str | None,
    secondary_block_title: str | None = None,
    secondary_block_body: str | None = None,
    action_label: str | None = None,
    action_url: str | None = None,
    footer: str,
    logo_cid: str | None = LOGO_CID,
) -> str:
    """Render the branded HTML body used by multipart notification emails."""
    style = _tone_style(tone)
    safe_action_url = action_url if _is_http_url(action_url) else None
    action_html = ""
    if safe_action_url and action_label:
        action_html = (
            '<tr><td style="padding:0 28px 28px 28px;background:#ffffff;">'
            f'<a href="{escape(safe_action_url, quote=True)}" '
            f'style="display:inline-block;background:{style["accent"]};color:#ffffff;'
            "text-decoration:none;font-size:14px;font-weight:700;padding:11px 18px;"
            'border-radius:6px;">'
            f"{escape(action_label)}</a>"
            "</td></tr>"
        )

    secondary_html = ""
    if secondary_block_title:
        secondary_html = _render_block(secondary_block_title, secondary_block_body, "info")

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        f"<title>{escape(title)}</title></head>"
        '<body style="margin:0;padding:0;background:#f4f6f8;'
        '-webkit-text-size-adjust:100%;font-family:Arial,Helvetica,sans-serif;">'
        '<span style="display:none!important;visibility:hidden;opacity:0;color:transparent;'
        'height:0;width:0;overflow:hidden;">'
        f"{escape(summary)}</span>"
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="background:#f4f6f8;margin:0;padding:0;">'
        '<tr><td align="center" style="padding:28px 12px;">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="max-width:680px;background:#ffffff;border-collapse:separate;'
        "border-spacing:0;border-radius:8px;overflow:hidden;"
        'box-shadow:0 12px 32px rgba(33,37,41,0.12);">'
        f'<tr><td style="height:5px;background:{style["accent"]};font-size:0;line-height:0;">'
        "&nbsp;</td></tr>"
        '<tr><td style="padding:26px 28px 18px 28px;background:#ffffff;">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0">'
        "<tr>"
        f'<td align="left" style="vertical-align:middle;">{_render_logo(logo_cid, product_name)}</td>'
        '<td align="right" style="vertical-align:middle;">'
        f'<span style="display:inline-block;background:{style["badge_bg"]};'
        f'color:{style["badge_text"]};border:1px solid {style["accent"]};'
        "border-radius:999px;padding:7px 12px;font-size:12px;font-weight:700;"
        'text-transform:uppercase;letter-spacing:0;">'
        f"{escape(status_label)}</span>"
        "</td></tr></table>"
        f'<div style="font-size:13px;color:#6c747c;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0;margin-top:24px;">{escape(eyebrow)}</div>'
        f'<h1 style="margin:8px 0 10px 0;color:#212529;font-size:26px;line-height:1.25;'
        f'font-weight:700;">{escape(title)}</h1>'
        f'<p style="margin:0;color:#495057;font-size:15px;line-height:1.6;">{escape(summary)}</p>'
        "</td></tr>"
        '<tr><td style="padding:0 28px 24px 28px;background:#ffffff;">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="border:1px solid #e9ecef;border-radius:6px;overflow:hidden;">'
        f"{_render_detail_rows(details)}"
        "</table></td></tr>"
        f"{_render_block(primary_block_title, primary_block_body, tone)}"
        f"{secondary_html}"
        f"{action_html}"
        '<tr><td style="padding:18px 28px 24px 28px;background:#f8f9fa;'
        'border-top:1px solid #e9ecef;color:#6c747c;font-size:12px;line-height:1.5;">'
        f"{escape(footer)}"
        "</td></tr>"
        "</table></td></tr></table></body></html>"
    )


def attach_inline_logo(message: EmailMessage, logo_path: Path, logo_cid: str) -> None:
    """Attach a logo file as an inline related image when an HTML part exists."""
    try:
        logo_bytes = logo_path.read_bytes()
    except OSError:
        return

    html_part = cast(EmailMessage | None, message.get_body(("html",)))
    if html_part is None:
        return

    html_part.add_related(
        logo_bytes,
        maintype="image",
        subtype="png",
        cid=f"<{logo_cid}>",
    )


def build_branded_email_message(
    *,
    subject: str,
    sender: str,
    recipient: str,
    text_body: str,
    product_name: str,
    eyebrow: str,
    title: str,
    summary: str,
    status_label: str,
    tone: str,
    details: DetailRows,
    primary_block_title: str,
    primary_block_body: str | None,
    secondary_block_title: str | None = None,
    secondary_block_body: str | None = None,
    action_label: str | None = None,
    action_url: str | None = None,
    footer: str = "This message was generated automatically by ESUP-Runner.",
    logo_path: Path | None = None,
) -> EmailMessage:
    """Build a multipart text/HTML email with optional inline logo branding."""
    resolved_logo_path = logo_path or DEFAULT_LOGO_PATH
    logo_cid = LOGO_CID if resolved_logo_path.is_file() else None
    html_body = render_html_email(
        product_name=product_name,
        eyebrow=eyebrow,
        title=title,
        summary=summary,
        status_label=status_label,
        tone=tone,
        details=details,
        primary_block_title=primary_block_title,
        primary_block_body=primary_block_body,
        secondary_block_title=secondary_block_title,
        secondary_block_body=secondary_block_body,
        action_label=action_label,
        action_url=action_url,
        footer=footer,
        logo_cid=logo_cid,
    )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    if logo_cid:
        attach_inline_logo(message, resolved_logo_path, logo_cid)

    return message
