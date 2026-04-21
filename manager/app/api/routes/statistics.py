# manager/app/api/routes/statistics.py
"""
Statistics routes for Runner Manager.
Reads data/task_stats.csv and renders statistics page.
"""

import csv
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates

from app.__version__ import __version__
from app.core.auth import verify_admin
from app.core.setup_logging import setup_default_logging

logger = setup_default_logging()

router = APIRouter(prefix="/statistics", tags=["Statistics"], dependencies=[Depends(verify_admin)])

templates = Jinja2Templates(directory="app/web/templates")


def _task_stats_csv_path() -> Path:
    """Return the canonical path of the statistics CSV file."""
    return Path("data") / "task_stats.csv"


def _load_task_stats_csv(csv_path: Path) -> List[Dict[str, str]]:
    """Load CSV task-stat rows, or return an empty list if unavailable."""
    if not csv_path.exists():
        return []
    try:
        with csv_path.open("r", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            return [row for row in reader]
    except Exception as exc:
        logger.error(f"Failed to read task stats CSV: {exc}")
        return []


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

    ordered_filter_dates = sorted(
        value for value in (start_date_value, end_date_value) if value is not None
    )
    start_date_value = (
        ordered_filter_dates[0] if len(ordered_filter_dates) == 2 else start_date_value
    )
    end_date_value = ordered_filter_dates[1] if len(ordered_filter_dates) == 2 else end_date_value

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
            "download_csv_url": request.url_for("download_task_stats_csv"),
        },
    )


@router.get("/task-stats.csv", include_in_schema=False)
async def download_task_stats_csv():
    """Download the raw task statistics CSV file."""
    csv_path = _task_stats_csv_path()
    if not csv_path.exists() or not csv_path.is_file():
        raise HTTPException(status_code=404, detail="Task stats CSV not found")

    download_date = datetime.now().strftime("%Y%m%d")
    return FileResponse(
        path=str(csv_path),
        media_type="text/csv",
        filename=f"task_stats_{download_date}.csv",
    )
