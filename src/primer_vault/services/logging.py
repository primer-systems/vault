"""
Logging - Application logging configuration and disk persistence.

Provides:
- Python logging configuration with console and optional file output
- Log persistence to daily files: primer-YYYY-MM-DD.log
- Automatic cleanup of old log files
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import logging
import os

from ..utils import get_logs_dir


def configure_logging(level: int = logging.INFO) -> None:
    """
    Configure Python logging for the application.

    Sets up a root logger with console output. File logging is handled
    separately by the log persistence functions.

    Log level can be set via PRIMER_LOG_LEVEL environment variable:
        DEBUG, INFO, WARNING, ERROR, CRITICAL

    Args:
        level: Logging level (default: INFO, overridden by env var)
    """
    root_logger = logging.getLogger()

    # Only configure if not already configured
    if root_logger.handlers:
        return

    # Check environment variable for log level
    env_level = os.environ.get("PRIMER_LOG_LEVEL", "").upper()
    if env_level:
        level = getattr(logging, env_level, level)

    root_logger.setLevel(level)

    # Console handler with simple format
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S'
    )
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)


def get_log_file_path(date: Optional[datetime] = None) -> Path:
    """Get the log file path for a specific date (defaults to today)."""
    if date is None:
        date = datetime.now()
    filename = f"primer-{date.strftime('%Y-%m-%d')}.log"
    return get_logs_dir() / filename


def append_log(message: str, retention_days: int = 0) -> None:
    """
    Append a log message to today's log file.

    Args:
        message: The log message (should already include timestamp)
        retention_days: If 0, don't save to disk
    """
    if retention_days <= 0:
        return

    log_path = get_log_file_path()
    try:
        with open(log_path, 'a', encoding='utf-8') as f:
            # Add full timestamp if message only has time
            if message.startswith('[') and len(message) > 10 and message[9] == ']':
                # Message has [HH:MM:SS] format, add date
                date_str = datetime.now().strftime('%Y-%m-%d')
                message = f"[{date_str} {message[1:]}"
            f.write(message + '\n')
    except Exception:
        # Silently fail - don't crash app for logging issues
        pass


def load_recent_logs(max_lines: int = 500) -> list[str]:
    """
    Load recent log lines from disk.

    Reads from today's log file, and if needed yesterday's,
    to get up to max_lines.

    Args:
        max_lines: Maximum number of lines to load (0 = don't load)

    Returns:
        List of log lines, oldest first
    """
    if max_lines <= 0:
        return []

    lines = []

    # Try today's file first
    today_path = get_log_file_path()
    if today_path.exists():
        lines = _read_last_n_lines(today_path, max_lines)

    # If we need more lines, try yesterday
    if len(lines) < max_lines:
        yesterday = datetime.now() - timedelta(days=1)
        yesterday_path = get_log_file_path(yesterday)
        if yesterday_path.exists():
            remaining = max_lines - len(lines)
            yesterday_lines = _read_last_n_lines(yesterday_path, remaining)
            lines = yesterday_lines + lines

    return lines


def _read_last_n_lines(file_path: Path, n: int) -> list[str]:
    """Read the last N lines from a file."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            all_lines = f.readlines()
            # Strip newlines and return last N
            return [line.rstrip('\n') for line in all_lines[-n:]]
    except Exception:
        return []


def cleanup_old_logs(retention_days: int) -> int:
    """
    Delete log files older than retention_days.

    Args:
        retention_days: Delete files older than this (0 = delete all)

    Returns:
        Number of files deleted
    """
    if retention_days < 0:
        return 0

    logs_dir = get_logs_dir()
    cutoff_date = datetime.now() - timedelta(days=retention_days)
    deleted_count = 0

    try:
        for file_path in logs_dir.glob("primer-*.log"):
            # Parse date from filename
            try:
                date_str = file_path.stem.replace("primer-", "")
                file_date = datetime.strptime(date_str, "%Y-%m-%d")

                if file_date < cutoff_date:
                    file_path.unlink()
                    deleted_count += 1
            except (ValueError, OSError):
                # Skip files that don't match expected format
                pass
    except Exception:
        pass

    return deleted_count


def format_log_for_display(log_line: str) -> str:
    """
    Convert a stored log line back to display format.

    Stored: [2026-02-08 14:32:15] Server started
    Display: [14:32:15] Server started
    """
    # If line has full datetime format, extract just the time
    if log_line.startswith('[') and len(log_line) > 20 and log_line[11] == ' ':
        # [2026-02-08 14:32:15] -> [14:32:15]
        time_part = log_line[12:20]
        rest = log_line[21:]
        return f"[{time_part}]{rest}"
    return log_line
