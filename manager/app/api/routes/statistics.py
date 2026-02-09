# manager/app/api/routes/statistics.py
"""
Statistics routes for Runner Manager.
Reads data/task_stats.csv and renders statistics page.
"""

import csv
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates

from app.__version__ import __version__
from app.core.auth import verify_admin
from app.core.setup_logging import setup_default_logging

logger = setup_default_logging()

router = APIRouter(prefix="/statistics", tags=["Statistics"], dependencies=[Depends(verify_admin)])

templates = Jinja2Templates(directory="app/web/templates")


def _load_task_stats_csv(csv_path: Path) -> List[Dict[str, str]]:
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
    return [
        {"label": label, "count": count}
        for label, count in sorted(counter.items(), key=lambda item: item[1], reverse=True)
    ]


@router.get("", include_in_schema=False)
async def statistics_dashboard(request: Request):
    csv_path = Path("data") / "task_stats.csv"
    rows = _load_task_stats_csv(csv_path)

    total_tasks = len(rows)

    by_type = Counter(row.get("task_type") or "unknown" for row in rows)
    by_etab = Counter(row.get("etab_name") or "unknown" for row in rows)
    by_date = Counter(row.get("date") or "unknown" for row in rows)

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
            "by_type": sorted_by_type,
            "by_etab": sorted_by_etab,
            "by_date": sorted_by_date,
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "version": __version__,
            "dark_mode_enabled": dark_mode,
            "csv_path": str(csv_path),
        },
    )
