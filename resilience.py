"""Pure-Python reliability helpers for the Fusion exporter.

There are intentionally no Autodesk API imports here.  Filesystem decisions,
crash recovery, and memory policy can therefore be tested outside Fusion and
reviewed independently from the API orchestration code.
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


GIB = 1024 ** 3
STATE_FILENAME = ".fusion360-export-state.json"
STATE_VERSION = 1


def bytes_as_gib(value: Optional[int]) -> str:
    if value is None:
        return "unknown"
    return f"{value / GIB:.1f} GiB"


def output_is_fresh(path: Path, source_mtime: float) -> bool:
    """Return true for a non-empty output at least as new as its cloud source."""
    try:
        stat = path.stat()
    except (FileNotFoundError, OSError):
        return False
    return stat.st_size > 0 and stat.st_mtime >= source_mtime


def partial_output_path(path: Path) -> Path:
    """Return a same-directory partial path ending in the real format suffix."""
    return path.with_name(f".{path.name}.partial{path.suffix}")


@dataclass(frozen=True)
class MemorySnapshot:
    process_rss: Optional[int]
    system_available: Optional[int]
    system_total: Optional[int]

    def describe(self) -> str:
        return (
            f"Fusion RSS {bytes_as_gib(self.process_rss)}, "
            f"system available {bytes_as_gib(self.system_available)}"
        )


class MemoryMonitor:
    """Measure process/system memory with no third-party dependencies."""

    def __init__(self, minimum_free_gib: int = 4, minimum_free_fraction: float = 0.10):
        self.minimum_free_bytes = minimum_free_gib * GIB
        self.minimum_free_fraction = minimum_free_fraction

    def sample(self) -> MemorySnapshot:
        system = platform.system()
        if system == "Darwin":
            return self._sample_macos()
        if system == "Windows":
            return self._sample_windows()
        if system == "Linux":
            return self._sample_linux()
        raise OSError(f"Memory monitoring is unsupported on {system}")

    def safety_floor(self, snapshot: MemorySnapshot) -> int:
        fractional_floor = 0
        if snapshot.system_total is not None:
            fractional_floor = int(snapshot.system_total * self.minimum_free_fraction)
        return max(self.minimum_free_bytes, fractional_floor)

    def is_low(self, snapshot: MemorySnapshot) -> bool:
        if snapshot.system_available is None:
            return False
        return snapshot.system_available < self.safety_floor(snapshot)

    @staticmethod
    def _sample_macos() -> MemorySnapshot:
        page_size = os.sysconf("SC_PAGE_SIZE")
        total = os.sysconf("SC_PHYS_PAGES") * page_size
        result = subprocess.run(
            ["/usr/bin/vm_stat"],
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
        )
        lines = result.stdout.splitlines()
        if not lines:
            raise OSError("vm_stat returned no output")
        match = re.search(r"page size of (\d+) bytes", lines[0])
        if match:
            page_size = int(match.group(1))

        pages: Dict[str, int] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            digits = re.sub(r"[^0-9]", "", value)
            if digits:
                pages[name] = int(digits)
        available_pages = sum(
            pages.get(name, 0)
            for name in ("Pages free", "Pages inactive", "Pages speculative")
        )
        return MemorySnapshot(
            process_rss=MemoryMonitor._macos_resident_size(),
            system_available=available_pages * page_size,
            system_total=total,
        )

    @staticmethod
    def _macos_resident_size() -> Optional[int]:
        class ProcessTaskInfo(ctypes.Structure):
            _fields_ = [
                ("virtual_size", ctypes.c_uint64),
                ("resident_size", ctypes.c_uint64),
                ("total_user", ctypes.c_uint64),
                ("total_system", ctypes.c_uint64),
                ("threads_user", ctypes.c_uint64),
                ("threads_system", ctypes.c_uint64),
                ("policy", ctypes.c_int),
                ("faults", ctypes.c_int),
                ("pageins", ctypes.c_int),
                ("cow_faults", ctypes.c_int),
                ("messages_sent", ctypes.c_int),
                ("messages_received", ctypes.c_int),
                ("syscalls_mach", ctypes.c_int),
                ("syscalls_unix", ctypes.c_int),
                ("context_switches", ctypes.c_int),
                ("thread_count", ctypes.c_int),
                ("running_thread_count", ctypes.c_int),
                ("priority", ctypes.c_int),
            ]

        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        libproc.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        libproc.proc_pidinfo.restype = ctypes.c_int
        info = ProcessTaskInfo()
        size = ctypes.sizeof(info)
        result = libproc.proc_pidinfo(
            os.getpid(), 4, 0, ctypes.byref(info), size  # PROC_PIDTASKINFO
        )
        return int(info.resident_size) if result == size else None

    @staticmethod
    def _sample_windows() -> MemorySnapshot:
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("page_fault_count", ctypes.c_ulong),
                ("peak_working_set_size", ctypes.c_size_t),
                ("working_set_size", ctypes.c_size_t),
                ("quota_peak_paged_pool_usage", ctypes.c_size_t),
                ("quota_paged_pool_usage", ctypes.c_size_t),
                ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
                ("quota_non_paged_pool_usage", ctypes.c_size_t),
                ("pagefile_usage", ctypes.c_size_t),
                ("peak_pagefile_usage", ctypes.c_size_t),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError("GlobalMemoryStatusEx failed")

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        process = ctypes.windll.kernel32.GetCurrentProcess()
        if not ctypes.windll.psapi.GetProcessMemoryInfo(
            process, ctypes.byref(counters), counters.cb
        ):
            raise OSError("GetProcessMemoryInfo failed")
        return MemorySnapshot(
            process_rss=int(counters.working_set_size),
            system_available=int(status.available_physical),
            system_total=int(status.total_physical),
        )

    @staticmethod
    def _sample_linux() -> MemorySnapshot:
        page_size = os.sysconf("SC_PAGE_SIZE")
        with open("/proc/self/statm", encoding="ascii") as statm:
            fields = statm.read().split()
        process_rss = int(fields[1]) * page_size
        values: Dict[str, int] = {}
        with open("/proc/meminfo", encoding="ascii") as meminfo:
            for line in meminfo:
                key, value = line.split(":", 1)
                values[key] = int(value.strip().split()[0]) * 1024
        return MemorySnapshot(
            process_rss=process_rss,
            system_available=values.get("MemAvailable"),
            system_total=values.get("MemTotal"),
        )


class ExportJournal:
    """Durably identifies a model which was active when Fusion terminated."""

    def __init__(self, output_folder: Path):
        self.path = output_folder / STATE_FILENAME
        state_file_existed = self.path.exists()
        self.state: Dict[str, Any] = {
            "version": STATE_VERSION,
            "in_progress": None,
            "quarantined": {},
        }
        self.recovered_record: Optional[Dict[str, Any]] = None
        self.load_error: Optional[str] = None
        self._load()
        interrupted = self.state.get("in_progress")
        if interrupted:
            record = dict(interrupted)
            record["quarantined_at"] = int(time.time())
            record["reason"] = "Fusion stopped while this model was in progress"
            self.state.setdefault("quarantined", {})[record["key"]] = record
            self.state["in_progress"] = None
            self.recovered_record = record
            self._save()
        elif not state_file_existed:
            record = self._recover_legacy_log()
            if record is not None:
                self.state["quarantined"][record["key"]] = record
                self.recovered_record = record
                self._save()

    def _recover_legacy_log(self) -> Optional[Dict[str, Any]]:
        """Recover the final open from a pre-journal exporter log, if present."""
        try:
            logs = sorted(
                (
                    path
                    for path in self.path.parent.glob("*.txt")
                    if re.fullmatch(
                        r"[0-9]{4}_[0-9]{2}_[0-9]{2}_[0-9]{2}_[0-9]{2}"
                        r"(?:_[0-9]{2})?\.txt",
                        path.name,
                    )
                ),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            if not logs:
                return None
            lines = logs[0].read_text(encoding="utf-8", errors="replace").splitlines()
            final_line = next((line.strip() for line in reversed(lines) if line.strip()), "")
            match = re.fullmatch(r"Opening `(.*)` v([0-9]+)", final_line)
            if match is None:
                return None
            name, version_text = match.groups()
            version = int(version_text)
            return {
                "key": f"legacy-log:{name}:v{version}",
                "name": name,
                "version": version,
                "quarantined_at": int(time.time()),
                "reason": f"Legacy log {logs[0].name} ended while opening this model",
            }
        except Exception as exc:
            self.load_error = f"Could not inspect legacy export logs in {self.path.parent}: {exc}"
            return None

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open(encoding="utf-8") as handle:
                loaded = json.load(handle)
            if not isinstance(loaded, dict):
                raise ValueError("journal root is not an object")
            if loaded.get("version") != STATE_VERSION:
                raise ValueError(f"unsupported journal version {loaded.get('version')}")
            if not isinstance(loaded.get("quarantined"), dict):
                raise ValueError("invalid quarantined collection")
            self.state = loaded
        except Exception as exc:
            self.load_error = f"Could not read {self.path}: {exc}"

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(self.state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def _matching_quarantine_keys(self, key: str, record=None):
        quarantined = self.state.get("quarantined", {})
        if key in quarantined:
            yield key
        if record is None:
            return
        for candidate_key, candidate in quarantined.items():
            if candidate_key == key:
                continue
            if (
                candidate.get("name") == record.get("name")
                and candidate.get("version") == record.get("version")
            ):
                yield candidate_key

    def is_quarantined(self, key: str, record=None) -> bool:
        return next(self._matching_quarantine_keys(key, record), None) is not None

    def retry(self, key: str, record=None) -> None:
        removed = False
        quarantined = self.state.get("quarantined", {})
        for candidate_key in list(self._matching_quarantine_keys(key, record)):
            quarantined.pop(candidate_key, None)
            removed = True
        if removed:
            self._save()

    def begin(self, record: Dict[str, Any]) -> None:
        self.state["in_progress"] = dict(record)
        self._save()

    def finish(self) -> None:
        if self.state.get("in_progress") is not None:
            self.state["in_progress"] = None
            self._save()
