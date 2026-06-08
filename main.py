from __future__ import annotations

import argparse
import asyncio
import ctypes
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

from job_bot.config import load_settings
from job_bot.coordinator import JobBotCoordinator
from job_bot.logger import setup_logging

WINDOWS_MUTEX_NAME = r"Local\JobsBotMainRuntimeLock"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smart jobs bot with Scrapling + OpenAI extraction.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one cycle only (useful for testing).",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Run browser fetchers in visible mode so you can watch actions.",
    )
    return parser.parse_args()


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _runtime_lock_details(lock_path: Path) -> str:
    try:
        content = lock_path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return ""
    return content


def _write_runtime_lock_metadata(lock_path: Path) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        f"pid={os.getpid()}\n"
        f"started_at_utc={datetime.now(timezone.utc).isoformat()}\n"
        f"command={sys.executable} {' '.join(sys.argv)}\n"
    )
    lock_path.write_text(payload, encoding="utf-8")


def _remove_runtime_lock_metadata(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except (FileNotFoundError, PermissionError, OSError):
        pass


def _acquire_windows_runtime_mutex() -> int | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    create_mutex.restype = ctypes.c_void_p
    handle = create_mutex(None, 0, WINDOWS_MUTEX_NAME)
    if not handle:
        raise OSError(f"CreateMutexW failed with error {ctypes.get_last_error()}")
    error_already_exists = 183
    if ctypes.get_last_error() == error_already_exists:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return None
    return int(handle)


def _release_windows_runtime_mutex(handle: int | None) -> None:
    if not handle:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle(ctypes.c_void_p(handle))


def _acquire_runtime_lock(lock_path: Path) -> int | None:
    if os.name == "nt":
        handle = _acquire_windows_runtime_mutex()
        if handle is None:
            return None
        _write_runtime_lock_metadata(lock_path)
        return handle
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
            return fd
        except FileExistsError:
            existing_pid: int | None = None
            try:
                existing_pid = int(lock_path.read_text(encoding="utf-8").strip())
            except (FileNotFoundError, ValueError, OSError):
                existing_pid = None
            if existing_pid is not None and _is_pid_alive(existing_pid):
                return None
            try:
                lock_path.unlink()
            except (FileNotFoundError, PermissionError, OSError):
                pass
    return None


def _release_runtime_lock(lock_path: Path, fd: int | None) -> None:
    if os.name == "nt":
        _remove_runtime_lock_metadata(lock_path)
        _release_windows_runtime_mutex(fd)
        return
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        lock_path.unlink()
    except (FileNotFoundError, PermissionError, OSError):
        pass


async def _run() -> None:
    cwd = Path(__file__).resolve().parent
    lock_path = cwd / "state" / "job_bot_main.lock"
    lock_fd = _acquire_runtime_lock(lock_path)
    if lock_fd is None:
        details = _runtime_lock_details(lock_path)
        extra = f"\n{details}" if details else ""
        print(
            f"Another scraper instance is already running. Lock file: {lock_path}{extra}",
            file=sys.stderr,
        )
        return
    try:
        args = parse_args()
        settings = load_settings(cwd)
        if args.headful:
            settings.headless_browser = False

        logger = setup_logging(
            settings.log_level,
            log_file_path=cwd / "state" / "runtime_logs" / "jobs-bot.txt",
        )
        logger.info(
            "Loaded settings | model=%s | websites=%s | agents=%s | output=%s | state_db=%s",
            settings.openai_model,
            len(settings.websites),
            settings.agent_count,
            settings.output_file_path,
            settings.state_db_path,
        )
        if args.headful:
            logger.info("Headful mode enabled. Browser windows will be visible.")

        coordinator = JobBotCoordinator(settings=settings, logger=logger)
        if args.once:
            await coordinator.run_once()
        else:
            await coordinator.run_forever()
    finally:
        _release_runtime_lock(lock_path, lock_fd)


if __name__ == "__main__":
    asyncio.run(_run())
