from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

_EMPTY_JSON_LITERALS: frozenset[str] = frozenset({"", "null", "none", "[]", "{}"})


def _serialize_json_field(value: Any, *, empty_fallback: str) -> str:
    """
    Normalize None/empty containers cleanly and serialize structured fields
    with `default=str` so non-JSON types (tuples, sets, Path, etc.) never fail.
    """
    if value is None:
        return empty_fallback
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped.lower() in _EMPTY_JSON_LITERALS:
            return empty_fallback
        try:
            parsed = json.loads(stripped)
            return json.dumps(parsed, default=str)
        except json.JSONDecodeError:
            return json.dumps(stripped, default=str)
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        if not items:
            return empty_fallback
        return json.dumps(items, default=str)
    if isinstance(value, dict):
        if not value:
            return empty_fallback
        return json.dumps(value, default=str)

    dumped = json.dumps(value, default=str)
    return empty_fallback if dumped.lower() in _EMPTY_JSON_LITERALS else dumped


def _clean_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


class YCLeadsDatabase:
    """SQLite persistence layer for YC companies, founders, and GTM signals."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 30000;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS companies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT NOT NULL UNIQUE,
                    yc_url TEXT NOT NULL,
                    company_name TEXT NOT NULL,
                    batch TEXT NOT NULL DEFAULT 'UNKNOWN',
                    status TEXT NOT NULL DEFAULT 'Active',
                    one_liner TEXT NOT NULL DEFAULT '',
                    verticals_json TEXT NOT NULL DEFAULT '[]',
                    website_url TEXT,
                    team_size INTEGER,
                    is_hiring INTEGER NOT NULL DEFAULT 0,
                    icp_score INTEGER NOT NULL DEFAULT 0,
                    icp_tier TEXT NOT NULL DEFAULT 'TIER_3_MONITOR',
                    raw_meta_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS founders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                    founder_name TEXT NOT NULL COLLATE NOCASE,
                    role TEXT NOT NULL DEFAULT 'Founder',
                    bio TEXT,
                    linkedin_url TEXT,
                    twitter_url TEXT,
                    is_priority_buyer INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE(company_id, founder_name)
                );
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS company_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                    signal_type TEXT NOT NULL,
                    signal_title TEXT NOT NULL,
                    signal_url TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE(company_id, signal_type, signal_title)
                );
                """
            )

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_companies_batch_tier ON companies(batch, icp_tier);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_companies_hiring ON companies(is_hiring, icp_score DESC);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_founders_company ON founders(company_id);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_signals_company ON company_signals(company_id);"
            )

    def upsert_company(self, company: dict[str, Any]) -> int:
        slug = str(company["slug"]).strip().lower()
        yc_url = str(company.get("yc_url") or f"https://www.ycombinator.com/companies/{slug}").strip()
        company_name = str(company.get("company_name") or slug.replace("-", " ").title()).strip()
        batch = str(company.get("batch") or "UNKNOWN").strip().upper()
        status = str(company.get("status") or "Active").strip()
        one_liner = str(company.get("one_liner") or "").strip()
        website_url = _clean_optional_text(company.get("website_url"))
        team_size = company.get("team_size")
        team_size_int = int(team_size) if team_size is not None else None
        is_hiring_int = 1 if bool(company.get("is_hiring", False)) else 0
        icp_score_int = int(company.get("icp_score", 0))
        icp_tier = str(company.get("icp_tier") or "TIER_3_MONITOR").strip()

        verticals_json = _serialize_json_field(
            company.get("verticals", company.get("verticals_json")),
            empty_fallback="[]",
        )
        raw_meta_json = _serialize_json_field(
            company.get("raw_meta", company.get("raw_meta_json")),
            empty_fallback="{}",
        )

        sql = """
            INSERT INTO companies (
                slug,
                yc_url,
                company_name,
                batch,
                status,
                one_liner,
                verticals_json,
                website_url,
                team_size,
                is_hiring,
                icp_score,
                icp_tier,
                raw_meta_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                yc_url = excluded.yc_url,
                company_name = CASE
                    WHEN excluded.company_name != '' THEN excluded.company_name
                    ELSE companies.company_name
                END,
                batch = CASE
                    WHEN excluded.batch != 'UNKNOWN' AND excluded.batch != '' THEN excluded.batch
                    ELSE companies.batch
                END,
                status = excluded.status,
                one_liner = CASE
                    WHEN excluded.one_liner != '' THEN excluded.one_liner
                    ELSE companies.one_liner
                END,
                verticals_json = CASE
                    WHEN excluded.verticals_json NOT IN ('null', 'none', '[]', '{}', '')
                    THEN excluded.verticals_json
                    ELSE companies.verticals_json
                END,
                website_url = COALESCE(excluded.website_url, companies.website_url),
                team_size = COALESCE(excluded.team_size, companies.team_size),
                is_hiring = excluded.is_hiring,
                icp_score = excluded.icp_score,
                icp_tier = excluded.icp_tier,
                raw_meta_json = CASE
                    WHEN excluded.raw_meta_json NOT IN ('null', 'none', '[]', '{}', '')
                    THEN excluded.raw_meta_json
                    ELSE companies.raw_meta_json
                END,
                updated_at = datetime('now')
            RETURNING id;
        """

        params = (
            slug,
            yc_url,
            company_name,
            batch,
            status,
            one_liner,
            verticals_json,
            website_url,
            team_size_int,
            is_hiring_int,
            icp_score_int,
            icp_tier,
            raw_meta_json,
        )

        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
            if row is None:
                raise RuntimeError(f"Failed to upsert company slug={slug!r}: no row returned")
            return int(row["id"])

    def upsert_founder(self, company_id: int, founder: dict[str, Any]) -> int:
        founder_name = str(founder.get("founder_name") or "").strip()
        if not founder_name:
            raise ValueError("founder_name must be a non-empty string")

        role = str(founder.get("role") or "Founder").strip()
        bio = _clean_optional_text(founder.get("bio"))
        linkedin_url = _clean_optional_text(founder.get("linkedin_url"))
        twitter_url = _clean_optional_text(founder.get("twitter_url"))
        is_priority_buyer_int = 1 if bool(founder.get("is_priority_buyer", False)) else 0

        sql = """
            INSERT INTO founders (
                company_id,
                founder_name,
                role,
                bio,
                linkedin_url,
                twitter_url,
                is_priority_buyer
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(company_id, founder_name) DO UPDATE SET
                role = CASE
                    WHEN excluded.role != '' THEN excluded.role
                    ELSE founders.role
                END,
                bio = COALESCE(excluded.bio, founders.bio),
                linkedin_url = COALESCE(excluded.linkedin_url, founders.linkedin_url),
                twitter_url = COALESCE(excluded.twitter_url, founders.twitter_url),
                is_priority_buyer = excluded.is_priority_buyer,
                updated_at = datetime('now')
            RETURNING id;
        """

        params = (
            int(company_id),
            founder_name,
            role,
            bio,
            linkedin_url,
            twitter_url,
            is_priority_buyer_int,
        )

        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
            if row is None:
                raise RuntimeError(
                    f"Failed to upsert founder={founder_name!r} for company_id={company_id}: no row returned"
                )
            return int(row["id"])

    def add_signal(
        self,
        company_id: int,
        signal_type: str,
        signal_title: str,
        signal_url: str | None = None,
    ) -> int | None:
        clean_type = str(signal_type).strip()
        clean_title = str(signal_title).strip()
        clean_url = _clean_optional_text(signal_url)
        if not clean_type or not clean_title:
            raise ValueError("signal_type and signal_title must be non-empty strings")

        sql = """
            INSERT INTO company_signals (
                company_id,
                signal_type,
                signal_title,
                signal_url
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(company_id, signal_type, signal_title) DO UPDATE SET
                signal_url = COALESCE(excluded.signal_url, company_signals.signal_url)
            RETURNING id;
        """

        with self.connect() as conn:
            row = conn.execute(sql, (int(company_id), clean_type, clean_title, clean_url)).fetchone()
            return int(row["id"]) if row is not None else None

    def summary_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM companies) AS companies_total,
                    (SELECT COUNT(*) FROM companies WHERE is_hiring = 1) AS companies_hiring,
                    (SELECT COUNT(*) FROM founders) AS founders_total,
                    (
                        SELECT COUNT(*)
                        FROM founders
                        WHERE linkedin_url IS NOT NULL AND TRIM(linkedin_url) != ''
                    ) AS founders_with_linkedin,
                    (SELECT COUNT(*) FROM company_signals) AS signals_total;
                """
            ).fetchone()

        if row is None:
            return {
                "companies_total": 0,
                "companies_hiring": 0,
                "founders_total": 0,
                "founders_with_linkedin": 0,
                "signals_total": 0,
            }

        return {
            "companies_total": int(row["companies_total"]),
            "companies_hiring": int(row["companies_hiring"]),
            "founders_total": int(row["founders_total"]),
            "founders_with_linkedin": int(row["founders_with_linkedin"]),
            "signals_total": int(row["signals_total"]),
        }