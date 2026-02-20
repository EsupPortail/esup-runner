import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# For FastAPI
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.__version__ import __version__
from app.core.auth import verify_admin
from app.core.config import config
from app.core.setup_logging import setup_default_logging

# Configure logging
logger = setup_default_logging()

# Create log router
router = APIRouter(prefix="/logs", tags=["Logs"], dependencies=[Depends(verify_admin)])

# Templates configuration
templates = Jinja2Templates(directory="app/web/templates")


class LogParser:
    """Parser for the specific log format"""

    LOG_PATTERN = re.compile(
        r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - "
        r"(?P<module>.+?) - "
        r"(?P<level>[A-Za-z]+) - "
        r"(?P<context>\[.*?\]) - "
        r"(?P<message>.*)$"
    )

    @staticmethod
    def _strip_line_end(line: str) -> str:
        """Strip line terminator while preserving leading/trailing visible spaces."""
        return line.rstrip("\r\n")

    @classmethod
    def parse_structured_log_line(cls, line: str) -> Optional[Dict[str, str]]:
        """Parse a line only if it matches the expected structured format."""
        match = cls.LOG_PATTERN.match(line)
        if not match:
            return None

        return {
            "timestamp": match.group("timestamp"),
            "module": match.group("module"),
            "level": match.group("level").upper(),
            "context": match.group("context"),
            "message": match.group("message"),
            "raw": line,
        }

    @staticmethod
    def create_unknown_log_line(line: str) -> Dict[str, str]:
        """Fallback for lines that don't match the structured pattern."""
        return {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "module": "UNKNOWN",
            "level": "UNKNOWN",
            "context": "",
            "message": line,
            "raw": line,
        }

    @classmethod
    def parse_log_line(cls, line: str) -> Dict[str, str]:
        """
        Parse a log line according to the format:
        2025-10-22 15:43:34 - runner - INFO - [encoding_handler:execute_task:134] - Encoding task completed successfully
        """
        normalized_line = cls._strip_line_end(line)
        parsed = cls.parse_structured_log_line(normalized_line)
        if parsed:
            return parsed
        return cls.create_unknown_log_line(normalized_line)


class LogManager:
    """Log manager for handling log files"""

    def __init__(self, log_paths: List[str]):
        self.log_paths = log_paths
        self.available_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

    @staticmethod
    def _append_line(existing: str, line: str) -> str:
        """Append a continuation line while preserving multi-line payloads."""
        return f"{existing}\n{line}" if existing else line

    def _parse_log_entries(self, lines: List[str]) -> List[Dict[str, str]]:
        """
        Parse file lines into logical log entries.
        Lines that do not match the structured pattern are treated as continuations
        of the previous structured entry (e.g. HTML payloads, stack traces).
        """
        entries: List[Dict[str, str]] = []
        current_entry: Optional[Dict[str, str]] = None

        for raw_line in lines:
            line = LogParser._strip_line_end(raw_line)
            structured_entry = LogParser.parse_structured_log_line(line)

            if structured_entry:
                if current_entry:
                    entries.append(current_entry)
                current_entry = structured_entry
                continue

            if current_entry:
                current_entry["message"] = self._append_line(current_entry["message"], line)
                current_entry["raw"] = self._append_line(current_entry["raw"], line)
                continue

            if not line:
                continue

            entries.append(LogParser.create_unknown_log_line(line))

        if current_entry:
            entries.append(current_entry)

        return entries

    def read_logs(
        self,
        limit: int = 1000,
        level_filter: Optional[List[str]] = None,
        search_term: Optional[str] = None,
    ) -> List[Dict]:
        """
        Read logs from configured files with optional filtering
        """
        all_logs = []

        for log_path in self.log_paths:
            log_file = Path(log_path)

            if not log_file.exists():
                logger.warning(f"Log file not found: {log_path}")
                continue

            try:
                # Read all lines from log file
                with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()

                # Parse each logical log entry (multi-line payloads included)
                for parsed_log in self._parse_log_entries(lines):

                    # Apply level filter if specified
                    if level_filter and parsed_log["level"] not in level_filter:
                        continue

                    # Apply search filter if specified
                    if search_term and search_term.lower() not in parsed_log["raw"].lower():
                        continue

                    all_logs.append(parsed_log)

            except Exception as e:
                logger.error(f"Error reading log file {log_path}: {e}")
                continue

        # Sort by timestamp (oldest first)
        all_logs.sort(key=lambda x: x["timestamp"])

        # Keep the most recent `limit` entries while preserving oldest->newest display
        return all_logs[-limit:]

    def get_logs_statistics(self, logs: List[Dict]) -> Dict[str, int]:
        """Calculate statistics by log level"""
        stats = {level: 0 for level in self.available_levels}
        stats["UNKNOWN"] = 0

        for log in logs:
            level = log["level"]
            stats[level] = stats.get(level, 0) + 1

        return stats


# Log paths configuration
# f"{config.LOG_DIRECTORY}uvicorn.log",
LOG_PATHS = [
    f"{config.LOG_DIRECTORY}manager.log",
]

# Initialize log manager
log_manager = LogManager(LOG_PATHS)


# ======================================================
# Endpoints
# ======================================================


@router.get("/", response_class=HTMLResponse)
async def view_logs(
    request: Request,
    limit: int = Query(1000, description="Maximum number of logs to display", ge=1, le=10000),
    level: List[str] = Query([], description="Filter by log level"),
    search: str = Query(None, description="Search term"),
    auto_refresh: int = Query(0, description="Auto-refresh interval in seconds"),
):
    """
    Main logs display page with filtering and search capabilities
    """
    try:
        # Read logs with applied filters
        logs = log_manager.read_logs(
            limit=limit, level_filter=level if level else None, search_term=search
        )

        # Calculate statistics
        stats = log_manager.get_logs_statistics(logs)

        dark_mode = request.cookies.get("theme") == "dark"

        # Prepare template context
        context = {
            "request": request,
            "logs": logs,
            "levels_count": stats,
            "total_logs": len(logs),
            "available_levels": log_manager.available_levels,
            "current_filters": {
                "limit": limit,
                "levels": level,
                "search": search,
                "auto_refresh": auto_refresh,
            },
            "dark_mode_enabled": dark_mode,
            "now": datetime.now(),
            "version": __version__,
        }

        return templates.TemplateResponse(request, "logs.html", context)

    except Exception as e:
        logger.error(f"Error displaying logs: {e}")
        raise HTTPException(status_code=500, detail="Error reading logs")


@router.get("/stream", response_class=HTMLResponse)
async def stream_logs(
    request: Request,
    limit: int = Query(100, description="Number of recent logs to display", ge=1, le=1000),
):
    """
    Endpoint for log streaming (used for auto-refresh)
    """
    try:
        logs = log_manager.read_logs(limit=limit)

        context = {"request": request, "logs": logs, "now": datetime.now()}

        return templates.TemplateResponse(request, "logs_partial.html", context)

    except Exception as e:
        logger.error(f"Error streaming logs: {e}")
        return HTMLResponse(content="<div class='alert alert-danger'>Error reading logs</div>")


@router.get("/search")
async def search_logs(
    request: Request,
    q: str = Query(..., description="Search term"),
    limit: int = Query(500, description="Maximum number of results"),
):
    """
    Advanced search in logs
    """
    try:
        logs = log_manager.read_logs(limit=limit, search_term=q)

        context = {
            "request": request,
            "logs": logs,
            "search_term": q,
            "total_results": len(logs),
            "now": datetime.now(),
        }

        return templates.TemplateResponse(request, "logs_search.html", context)

    except Exception as e:
        logger.error(f"Error searching logs: {e}")
        raise HTTPException(status_code=500, detail="Error during search")


@router.get("/api/stats")
async def get_logs_stats():
    """
    API endpoint for log statistics (for dashboard)
    """
    try:
        # Read recent logs for statistics
        logs = log_manager.read_logs(limit=5000)
        stats = log_manager.get_logs_statistics(logs)

        return {"total": len(logs), "by_level": stats, "last_updated": datetime.now().isoformat()}

    except Exception as e:
        logger.error(f"Error getting log statistics: {e}")
        raise HTTPException(status_code=500, detail="Error calculating statistics")


def tail_logs(file_path: str, n: int = 100) -> List[str]:
    """
    Read the last n lines of a file (similar to tail command)
    Useful for real-time log monitoring
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            return lines[-n:]
    except Exception as e:
        logger.error(f"Error tailing logs {file_path}: {e}")
        return []
