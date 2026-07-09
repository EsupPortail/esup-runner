# manager/app/api/routes/statistics.py
"""
Statistics routes for Runner Manager.
Reads data/task_stats.csv and renders statistics page.
"""

import csv
from collections import Counter
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from typing import Dict, List
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.templating import Jinja2Templates

from app.__version__ import __version__
from app.core.auth import verify_admin
from app.core.setup_logging import setup_default_logging

logger = setup_default_logging()

router = APIRouter(prefix="/statistics", tags=["Statistics"], dependencies=[Depends(verify_admin)])

templates = Jinja2Templates(directory="app/web/templates")

_TASK_STATS_DEFAULT_FIELDNAMES = [
    "task_id",
    "date",
    "task_type",
    "status",
    "app_name",
    "app_version",
    "etab_name",
]


def _task_stats_csv_path() -> Path:
    """Return the canonical path of the statistics CSV file."""
    return Path("data") / "task_stats.csv"


def _load_task_stats_csv(csv_path: Path) -> List[Dict[str, str]]:
    """Load CSV task-stat rows, or return an empty list if unavailable."""
    rows, _fieldnames = _load_task_stats_csv_with_fieldnames(csv_path)
    return rows


def _load_task_stats_csv_with_fieldnames(csv_path: Path) -> tuple[List[Dict[str, str]], List[str]]:
    """Load CSV task-stat rows and field names, or return empty values if unavailable."""
    if not csv_path.exists():
        return [], []
    try:
        with csv_path.open("r", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            return [row for row in reader], list(reader.fieldnames or [])
    except Exception as exc:
        logger.error(f"Failed to read task stats CSV: {exc}")
        return [], []


def _sorted_counter(counter: Counter) -> List[Dict[str, int]]:
    """Convert a ``Counter`` to a descending label/count list."""
    return [
        {"label": label, "count": count}
        for label, count in sorted(counter.items(), key=lambda item: item[1], reverse=True)
    ]


def _parse_iso_date(value: str | None) -> date | None:
    """Parse an ISO date (``YYYY-MM-DD``), returning ``None`` when invalid."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _available_date_bounds(rows: List[Dict[str, str]]) -> tuple[date | None, date | None]:
    """Return the minimum and maximum valid dates found in CSV rows."""
    valid_dates: List[date] = []
    for row in rows:
        row_date = _parse_iso_date(row.get("date"))
        if row_date is not None:
            valid_dates.append(row_date)

    valid_dates.sort()
    return (
        valid_dates[0] if valid_dates else None,
        valid_dates[-1] if valid_dates else None,
    )


def _filter_rows_by_date_range(
    rows: List[Dict[str, str]], start_date: date | None, end_date: date | None
) -> List[Dict[str, str]]:
    """Filter CSV rows by an inclusive date range when a period is provided."""
    if start_date is None and end_date is None:
        return rows

    filtered_rows: List[Dict[str, str]] = []
    for row in rows:
        row_date = _parse_iso_date(row.get("date"))
        if row_date is None:
            continue
        if start_date is not None and row_date < start_date:
            continue
        if end_date is not None and row_date > end_date:
            continue
        filtered_rows.append(row)
    return filtered_rows


def _normalize_date_range(
    start_date: date | None, end_date: date | None
) -> tuple[date | None, date | None]:
    """Return a normalized inclusive date range."""
    ordered_filter_dates = sorted(value for value in (start_date, end_date) if value is not None)
    if len(ordered_filter_dates) != 2:
        return start_date, end_date
    return ordered_filter_dates[0], ordered_filter_dates[1]


def _build_filtered_csv_url(
    request: Request,
    start_date: date | None,
    end_date: date | None,
) -> str:
    """Return the CSV download URL, preserving active date filters."""
    base_url = str(request.url_for("download_task_stats_csv"))
    params: Dict[str, str] = {}
    if start_date is not None:
        params["start_date"] = start_date.isoformat()
    if end_date is not None:
        params["end_date"] = end_date.isoformat()
    if not params:
        return base_url
    return f"{base_url}?{urlencode(params)}"


def _csv_rows_to_text(rows: List[Dict[str, str]], fieldnames: List[str]) -> str:
    """Render task-stat rows to CSV text."""
    output = StringIO()
    effective_fieldnames = fieldnames or _TASK_STATS_DEFAULT_FIELDNAMES
    writer = csv.DictWriter(
        output,
        fieldnames=effective_fieldnames,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _download_filename(
    download_date: str,
    start_date: date | None,
    end_date: date | None,
) -> str:
    """Return a clear download filename for raw or filtered CSV exports."""
    parts = [download_date]
    if start_date is not None:
        parts.append(f"from_{start_date.isoformat().replace('-', '')}")
    if end_date is not None:
        parts.append(f"to_{end_date.isoformat().replace('-', '')}")
    return f"task_stats_{'_'.join(parts)}.csv"


@router.get("", include_in_schema=False)
async def statistics_dashboard(
    request: Request,
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
):
    """Render the statistics dashboard with optional date-range filtering."""
    start_date_value = _parse_iso_date(start_date)
    end_date_value = _parse_iso_date(end_date)

    csv_path = _task_stats_csv_path()
    rows = _load_task_stats_csv(csv_path)
    available_start_date, available_end_date = _available_date_bounds(rows)

    start_date_value, end_date_value = _normalize_date_range(start_date_value, end_date_value)

    filtered_rows = _filter_rows_by_date_range(rows, start_date_value, end_date_value)
    total_tasks = len(filtered_rows)

    by_type = Counter(row.get("task_type") or "unknown" for row in filtered_rows)
    by_etab = Counter(row.get("etab_name") or "unknown" for row in filtered_rows)
    by_date = Counter(row.get("date") or "unknown" for row in filtered_rows)

    sorted_by_type = _sorted_counter(by_type)
    sorted_by_etab = _sorted_counter(by_etab)
    sorted_by_date = [
        {"label": label, "count": count}
        for label, count in sorted(by_date.items(), key=lambda item: item[0])
    ]

    date_labels = [row["label"] for row in sorted_by_date if row["label"] != "unknown"]
    date_range = None
    if date_labels:
        date_range = f"{date_labels[0]} → {date_labels[-1]}"

    selected_date_range = None
    if start_date_value is not None or end_date_value is not None:
        selected_start = start_date_value.isoformat() if start_date_value is not None else "..."
        selected_end = end_date_value.isoformat() if end_date_value is not None else "..."
        selected_date_range = f"{selected_start} → {selected_end}"

    dark_mode = request.cookies.get("theme") == "dark"

    return templates.TemplateResponse(
        request,
        "statistics.html",
        {
            "request": request,
            "total_tasks": total_tasks,
            "unique_types": len(by_type),
            "unique_etabs": len(by_etab),
            "date_range": date_range,
            "selected_date_range": selected_date_range,
            "selected_start_date": (
                start_date_value.isoformat() if start_date_value is not None else ""
            ),
            "selected_end_date": end_date_value.isoformat() if end_date_value is not None else "",
            "available_start_date": (
                available_start_date.isoformat() if available_start_date is not None else ""
            ),
            "available_end_date": (
                available_end_date.isoformat() if available_end_date is not None else ""
            ),
            "is_date_filter_active": start_date_value is not None or end_date_value is not None,
            "by_type": sorted_by_type,
            "by_etab": sorted_by_etab,
            "by_date": sorted_by_date,
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "version": __version__,
            "dark_mode_enabled": dark_mode,
            "csv_path": str(csv_path),
            "statistics_url": request.url_for("statistics_dashboard"),
            "download_csv_url": _build_filtered_csv_url(
                request,
                start_date_value,
                end_date_value,
            ),
            "download_csv_label": (
                "Download filtered CSV"
                if start_date_value is not None or end_date_value is not None
                else "Download CSV"
            ),
        },
    )


@router.get("/task-stats.csv", include_in_schema=False)
async def download_task_stats_csv(
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
):
    """Download the raw task statistics CSV file."""
    csv_path = _task_stats_csv_path()
    if not csv_path.exists() or not csv_path.is_file():
        raise HTTPException(status_code=404, detail="Task stats CSV not found")

    start_date_value, end_date_value = _normalize_date_range(
        _parse_iso_date(start_date),
        _parse_iso_date(end_date),
    )
    download_date = datetime.now().strftime("%Y%m%d")
    if start_date_value is not None or end_date_value is not None:
        rows, fieldnames = _load_task_stats_csv_with_fieldnames(csv_path)
        filtered_rows = _filter_rows_by_date_range(rows, start_date_value, end_date_value)
        filename = _download_filename(download_date, start_date_value, end_date_value)
        return Response(
            content=_csv_rows_to_text(filtered_rows, fieldnames),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return FileResponse(
        path=str(csv_path),
        media_type="text/csv",
        filename=_download_filename(download_date, None, None),
    )
