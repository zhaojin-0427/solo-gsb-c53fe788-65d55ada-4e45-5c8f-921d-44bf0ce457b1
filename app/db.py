"""PostgreSQL persistence layer (psycopg 3).

If the database is temporarily unreachable the application still starts:
schema creation and queries are guarded, and the UI reports storage status —
this keeps the workbench usable in restricted CI environments while the
documented deployment (docker compose) always has Postgres available.
"""
from __future__ import annotations

from typing import Any, Optional

import psycopg
from psycopg.types.json import Json

from .config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analyses (
    id              TEXT PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    baseline_name   TEXT NOT NULL,
    candidate_name  TEXT NOT NULL,
    baseline_meta   JSONB NOT NULL,
    candidate_meta  JSONB NOT NULL,
    report          JSONB NOT NULL,
    stats           JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS analyses_created_idx ON analyses (created_at DESC);

CREATE TABLE IF NOT EXISTS analysis_packages (
    id          TEXT PRIMARY KEY
                REFERENCES analyses(id) ON DELETE CASCADE,
    baseline    JSONB NOT NULL,
    candidate   JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_coverage (
    id          TEXT PRIMARY KEY
                REFERENCES analyses(id) ON DELETE CASCADE,
    baseline    JSONB,
    candidate   JSONB,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS analysis_migration (
    id          TEXT PRIMARY KEY
                REFERENCES analyses(id) ON DELETE CASCADE,
    doc         JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS release_exceptions (
    analysis_id     TEXT NOT NULL
                    REFERENCES analyses(id) ON DELETE CASCADE,
    report_version  TEXT NOT NULL,
    finding_id      TEXT NOT NULL,
    reason          TEXT NOT NULL,
    expires_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (analysis_id, report_version, finding_id)
);

CREATE INDEX IF NOT EXISTS release_exceptions_analysis_idx
    ON release_exceptions (analysis_id, report_version);
"""


class Database:
    def __init__(self, url: Optional[str] = None):
        self.url = url or settings.database_url
        self.available = False
        self.last_error: Optional[str] = None

    def connect(self) -> "Database":
        try:
            with psycopg.connect(self.url, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute(_SCHEMA)
                conn.commit()
            self.available = True
            self.last_error = None
        except Exception as exc:  # pragma: no cover - environment dependent
            self.available = False
            self.last_error = str(exc)
        return self

    def _conn(self):
        return psycopg.connect(self.url, connect_timeout=5)

    def save_analysis(self, analysis_id: str, baseline_name: str,
                      candidate_name: str, baseline_meta: dict,
                      candidate_meta: dict, report: dict,
                      stats: dict) -> bool:
        if not self.available:
            return False
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO analyses (id, baseline_name, candidate_name,
                                             baseline_meta, candidate_meta,
                                             report, stats)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            report = EXCLUDED.report,
                            stats = EXCLUDED.stats,
                            created_at = now()
                        """,
                        (analysis_id, baseline_name, candidate_name,
                         Json(baseline_meta), Json(candidate_meta),
                         Json(report), Json(stats)))
                conn.commit()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def list_analyses(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.available:
            return []
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, created_at, baseline_name, candidate_name,
                               baseline_meta, candidate_meta, stats
                        FROM analyses
                        ORDER BY created_at DESC, id
                        LIMIT %s
                        """, (limit,))
                    rows = cur.fetchall()
            out = []
            for r in rows:
                out.append({
                    "id": r[0],
                    "created_at": r[1].isoformat() if r[1] else None,
                    "baseline_name": r[2],
                    "candidate_name": r[3],
                    "baseline_meta": r[4],
                    "candidate_meta": r[5],
                    "stats": r[6],
                })
            return out
        except Exception as exc:
            self.last_error = str(exc)
            return []

    def get_analysis(self, analysis_id: str) -> Optional[dict[str, Any]]:
        if not self.available:
            return None
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, created_at, baseline_name, candidate_name,"
                        " baseline_meta, candidate_meta, report, stats "
                        "FROM analyses WHERE id = %s", (analysis_id,))
                    r = cur.fetchone()
            if not r:
                return None
            return {
                "id": r[0],
                "created_at": r[1].isoformat() if r[1] else None,
                "baseline_name": r[2],
                "candidate_name": r[3],
                "baseline_meta": r[4],
                "candidate_meta": r[5],
                "report": r[6],
                "stats": r[7],
            }
        except Exception as exc:
            self.last_error = str(exc)
            return None

    def delete_analysis(self, analysis_id: str) -> bool:
        if not self.available:
            return False
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM analyses WHERE id = %s",
                                (analysis_id,))
                conn.commit()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def save_packages(self, analysis_id: str, baseline: dict,
                      candidate: dict) -> bool:
        if not self.available:
            return False
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO analysis_packages (id, baseline, candidate)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            baseline = EXCLUDED.baseline,
                            candidate = EXCLUDED.candidate
                        """,
                        (analysis_id, Json(baseline), Json(candidate)))
                conn.commit()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def get_packages(self, analysis_id: str) -> Optional[dict[str, Any]]:
        if not self.available:
            return None
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT baseline, candidate FROM analysis_packages "
                        "WHERE id = %s", (analysis_id,))
                    r = cur.fetchone()
            if not r:
                return None
            return {"baseline": r[0], "candidate": r[1]}
        except Exception as exc:
            self.last_error = str(exc)
            return None

    def save_coverage(self, analysis_id: str, side: str,
                      doc: dict) -> bool:
        if not self.available:
            return False
        col = "baseline" if side == "baseline" else "candidate"
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        INSERT INTO analysis_coverage (id, {col}, updated_at)
                        VALUES (%s, %s, now())
                        ON CONFLICT (id) DO UPDATE SET
                            {col} = EXCLUDED.{col},
                            updated_at = now()
                        """,
                        (analysis_id, Json(doc)))
                conn.commit()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def get_coverage(self, analysis_id: str) -> Optional[dict[str, Any]]:
        if not self.available:
            return None
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT baseline, candidate FROM analysis_coverage "
                        "WHERE id = %s", (analysis_id,))
                    r = cur.fetchone()
            if not r:
                return None
            return {"baseline": r[0], "candidate": r[1]}
        except Exception as exc:
            self.last_error = str(exc)
            return None

    def delete_coverage(self, analysis_id: str, side: str) -> bool:
        if not self.available:
            return False
        col = "baseline" if side == "baseline" else "candidate"
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE analysis_coverage SET {col} = NULL, "
                        f"updated_at = now() WHERE id = %s",
                        (analysis_id,))
                conn.commit()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def save_migration(self, analysis_id: str, doc: dict) -> bool:
        if not self.available:
            return False
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO analysis_migration (id, doc, updated_at)
                        VALUES (%s, %s, now())
                        ON CONFLICT (id) DO UPDATE SET
                            doc = EXCLUDED.doc,
                            updated_at = now()
                        """,
                        (analysis_id, Json(doc)))
                conn.commit()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def get_migration(self, analysis_id: str) -> Optional[dict[str, Any]]:
        if not self.available:
            return None
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT doc FROM analysis_migration WHERE id = %s",
                        (analysis_id,))
                    r = cur.fetchone()
            if not r:
                return None
            return r[0]
        except Exception as exc:
            self.last_error = str(exc)
            return None

    def delete_migration(self, analysis_id: str) -> bool:
        if not self.available:
            return False
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM analysis_migration WHERE id = %s",
                        (analysis_id,))
                conn.commit()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def save_release_exception(self, exception: dict) -> bool:
        if not self.available:
            return False
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO release_exceptions
                            (analysis_id, report_version, finding_id, reason,
                             expires_at, created_at)
                        VALUES (%s, %s, %s, %s, %s, COALESCE(%s::timestamptz, now()))
                        ON CONFLICT (analysis_id, report_version, finding_id)
                        DO UPDATE SET
                            reason = EXCLUDED.reason,
                            expires_at = EXCLUDED.expires_at,
                            created_at = now()
                        """,
                        (exception["analysis_id"],
                         exception["report_version"],
                         exception["finding_id"],
                         exception["reason"],
                         exception["expires_at"],
                         exception.get("created_at")))
                conn.commit()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def list_release_exceptions(self, analysis_id: str) -> list[dict[str, Any]]:
        if not self.available:
            return []
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT analysis_id, report_version, finding_id, reason,
                               expires_at, created_at
                        FROM release_exceptions
                        WHERE analysis_id = %s
                        ORDER BY created_at DESC, finding_id, report_version
                        """,
                        (analysis_id,))
                    rows = cur.fetchall()
            return [{
                "analysis_id": r[0],
                "report_version": r[1],
                "finding_id": r[2],
                "reason": r[3],
                "expires_at": r[4].isoformat() if r[4] else None,
                "created_at": r[5].isoformat() if r[5] else None,
            } for r in rows]
        except Exception as exc:
            self.last_error = str(exc)
            return []

    def status(self) -> dict:
        return {"available": self.available, "error": self.last_error}


db = Database()
