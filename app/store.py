"""Storage facade: PostgreSQL when available, in-memory fallback otherwise.

The fallback keeps the workbench fully functional in environments without a
database (local try-outs, CI); the docker-compose deployment always uses
PostgreSQL and reports it via ``/api/health``.
"""
from __future__ import annotations

import threading
from typing import Any, Optional

from .db import db


class _MemoryStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._analyses: dict[str, dict] = {}
        self._packages: dict[str, dict] = {}
        self._coverage: dict[str, dict] = {}
        self._migration: dict[str, dict] = {}
        self._release_exceptions: dict[tuple[str, str, str], dict] = {}

    @property
    def available(self) -> bool:
        return False  # signals degraded mode

    def status(self) -> dict:
        return {"available": False, "mode": "memory",
                "error": "PostgreSQL 不可用，使用进程内存储（重启丢失）"}

    def save(self, analysis: dict, packages: dict) -> bool:
        with self._lock:
            self._analyses[analysis["id"]] = analysis
            self._packages[analysis["id"]] = packages
        return True

    def list_analyses(self, limit: int = 50):
        with self._lock:
            items = sorted(self._analyses.values(),
                           key=lambda a: a.get("created_at", ""),
                           reverse=True)
        return [{"id": a["id"],
                 "created_at": a.get("created_at"),
                 "baseline_name": a["baseline_name"],
                 "candidate_name": a["candidate_name"],
                 "baseline_meta": a["baseline_meta"],
                 "candidate_meta": a["candidate_meta"],
                 "stats": a["stats"]} for a in items[:limit]]

    def get(self, analysis_id: str) -> Optional[dict]:
        with self._lock:
            return self._analyses.get(analysis_id)

    def get_packages(self, analysis_id: str) -> Optional[dict]:
        with self._lock:
            return self._packages.get(analysis_id)

    def save_coverage(self, analysis_id: str, side: str, doc: dict) -> None:
        with self._lock:
            entry = self._coverage.setdefault(analysis_id, {})
            entry[side] = doc

    def get_coverage(self, analysis_id: str) -> Optional[dict]:
        with self._lock:
            entry = self._coverage.get(analysis_id)
            return dict(entry) if entry else None

    def delete_coverage(self, analysis_id: str, side: str) -> None:
        with self._lock:
            entry = self._coverage.get(analysis_id)
            if entry:
                entry.pop(side, None)

    def save_migration(self, analysis_id: str, doc: dict) -> None:
        with self._lock:
            self._migration[analysis_id] = doc

    def get_migration(self, analysis_id: str) -> Optional[dict]:
        with self._lock:
            return self._migration.get(analysis_id)

    def delete_migration(self, analysis_id: str) -> None:
        with self._lock:
            self._migration.pop(analysis_id, None)

    def save_release_exception(self, exception: dict) -> None:
        key = (exception["analysis_id"], exception["report_version"],
               exception["finding_id"])
        with self._lock:
            self._release_exceptions[key] = dict(exception)

    def list_release_exceptions(self, analysis_id: str) -> list[dict]:
        with self._lock:
            return [dict(v) for k, v in self._release_exceptions.items()
                    if k[0] == analysis_id]

    def delete(self, analysis_id: str) -> bool:
        with self._lock:
            existed = analysis_id in self._analyses
            self._analyses.pop(analysis_id, None)
            self._packages.pop(analysis_id, None)
            self._coverage.pop(analysis_id, None)
            self._migration.pop(analysis_id, None)
            for key in [k for k in self._release_exceptions
                        if k[0] == analysis_id]:
                self._release_exceptions.pop(key, None)
        return existed


_memory = _MemoryStore()


class Store:
    def __init__(self):
        self.db = db

    @property
    def is_postgres(self) -> bool:
        return self.db.available

    def status(self) -> dict:
        if self.db.available:
            s = self.db.status()
            return {"available": True, "mode": "postgresql",
                    "error": s.get("error")}
        return _memory.status()

    def save(self, *, analysis_id: str, baseline_name: str,
             candidate_name: str, baseline_meta: dict,
             candidate_meta: dict, report_doc: dict, stats: dict,
             created_at: str, packages: dict) -> None:
        if self.db.available:
            self.db.save_analysis(
                analysis_id, baseline_name, candidate_name,
                baseline_meta, candidate_meta, report_doc, stats)
            self.db.save_packages(analysis_id, packages["baseline"],
                                  packages["candidate"])
            if not self.db.available:
                # connection dropped mid-request -> degrade
                _memory.save(self._record(
                    analysis_id, baseline_name, candidate_name,
                    baseline_meta, candidate_meta, report_doc, stats,
                    created_at), packages)
        else:
            _memory.save(self._record(
                analysis_id, baseline_name, candidate_name,
                baseline_meta, candidate_meta, report_doc, stats,
                created_at), packages)

    @staticmethod
    def _record(analysis_id, baseline_name, candidate_name, baseline_meta,
                candidate_meta, report_doc, stats, created_at):
        return {"id": analysis_id, "created_at": created_at,
                "baseline_name": baseline_name,
                "candidate_name": candidate_name,
                "baseline_meta": baseline_meta,
                "candidate_meta": candidate_meta,
                "report": report_doc, "stats": stats}

    def list_analyses(self, limit: int = 50):
        if self.db.available:
            return self.db.list_analyses(limit)
        return _memory.list_analyses(limit)

    def get(self, analysis_id: str) -> Optional[dict[str, Any]]:
        if self.db.available:
            return self.db.get_analysis(analysis_id)
        return _memory.get(analysis_id)

    def get_packages(self, analysis_id: str) -> Optional[dict]:
        if self.db.available:
            return self.db.get_packages(analysis_id)
        return _memory.get_packages(analysis_id)

    def save_coverage(self, analysis_id: str, side: str, doc: dict) -> None:
        if self.db.available:
            ok = self.db.save_coverage(analysis_id, side, doc)
            if not ok:
                _memory.save_coverage(analysis_id, side, doc)
        else:
            _memory.save_coverage(analysis_id, side, doc)

    def get_coverage(self, analysis_id: str) -> Optional[dict]:
        if self.db.available:
            return self.db.get_coverage(analysis_id)
        return _memory.get_coverage(analysis_id)

    def delete_coverage(self, analysis_id: str, side: str) -> None:
        if self.db.available:
            self.db.delete_coverage(analysis_id, side)
        else:
            _memory.delete_coverage(analysis_id, side)

    def save_migration(self, analysis_id: str, doc: dict) -> None:
        if self.db.available:
            ok = self.db.save_migration(analysis_id, doc)
            if not ok:
                _memory.save_migration(analysis_id, doc)
        else:
            _memory.save_migration(analysis_id, doc)

    def get_migration(self, analysis_id: str) -> Optional[dict]:
        if self.db.available:
            return self.db.get_migration(analysis_id)
        return _memory.get_migration(analysis_id)

    def delete_migration(self, analysis_id: str) -> None:
        if self.db.available:
            self.db.delete_migration(analysis_id)
        else:
            _memory.delete_migration(analysis_id)

    def save_release_exception(self, exception: dict) -> None:
        if self.db.available:
            ok = self.db.save_release_exception(exception)
            if not ok:
                _memory.save_release_exception(exception)
        else:
            _memory.save_release_exception(exception)

    def list_release_exceptions(self, analysis_id: str) -> list[dict]:
        if self.db.available:
            rows = self.db.list_release_exceptions(analysis_id)
            # Do not silently lose audit history if the database drops during
            # a request: combine with any degraded in-memory rows.
            seen = {(r["analysis_id"], r["report_version"],
                     r["finding_id"]) for r in rows}
            rows.extend(r for r in _memory.list_release_exceptions(analysis_id)
                        if (r["analysis_id"], r["report_version"],
                            r["finding_id"]) not in seen)
            return rows
        return _memory.list_release_exceptions(analysis_id)

    def delete(self, analysis_id: str) -> bool:
        if self.db.available:
            return self.db.delete_analysis(analysis_id)
        return _memory.delete(analysis_id)


store = Store()
