import os
import subprocess
import sys
import threading
import logging

logger = logging.getLogger(__name__)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _get_lock(date_str: str) -> threading.Lock:
    with _locks_guard:
        if date_str not in _locks:
            _locks[date_str] = threading.Lock()
        return _locks[date_str]


def is_running(date_str: str) -> bool:
    lock = _get_lock(date_str)
    acquired = lock.acquire(blocking=False)
    if acquired:
        lock.release()
        return False
    return True


def run_pipeline(date_str: str):
    lock = _get_lock(date_str)
    if not lock.acquire(blocking=False):
        raise RuntimeError("already running")

    try:
        steps = [
            ["ingest/run_ingest.py", "--date", date_str],
            ["models/predict.py", "--date", date_str],
            ["recs/run_recs.py", "--date", date_str],
        ]
        for step in steps:
            result = subprocess.run(
                [sys.executable] + step,
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
            )
            logger.info("Pipeline step %s stdout: %s", step[0], result.stdout)
            if result.returncode != 0:
                logger.error("Pipeline step %s failed: %s", step[0], result.stderr)
                raise subprocess.CalledProcessError(result.returncode, step[0], result.stderr)
    finally:
        lock.release()
        with _locks_guard:
            _locks.pop(date_str, None)
