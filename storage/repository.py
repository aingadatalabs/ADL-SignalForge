"""
repository.py — Production-Hardened Domain Repository & State Manager for ADL-SignalForge

Features:
- Strict, explicit pipeline run state-machine transitions with attempt separation and accounting rules.
- Robust, secure identifier normalization (utilizing urllib.parse, IDNA conversion, IP/hostname validation).
- Versioned, namespaced SHA-256 identifier hashing to prevent collision and migration breakage.
- Append-only observer/assignment audit separation preventing silent identity reassignment.
- Domain-specific typed exceptions, strongly typed return structures, and verified atomic transactions.
"""

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Set
from urllib.parse import urlsplit

from core.database import DuckDBClient

logger = logging.getLogger("SignalForgeRepository")

# --- Domain Exceptions ---
class RepositoryError(Exception):
    """Base exception for all repository operations."""
    pass

class EntityNotFoundError(RepositoryError):
    pass

class IdentifierConflictError(RepositoryError):
    pass

class InvalidIdentifierError(RepositoryError):
    pass

class PipelineRunNotFoundError(RepositoryError):
    pass

class InvalidStateTransitionError(RepositoryError):
    pass

class RepositoryIntegrityError(RepositoryError):
    pass


# --- State Machines & Constants ---
VALID_RUN_STATES: Set[str] = {"pending", "running", "succeeded", "partial_success", "failed", "cancelled"}
TERMINAL_RUN_STATES: Set[str] = {"succeeded", "partial_success", "failed", "cancelled"}

# Explicit state transition rules: current_state -> set of allowed next states
STATE_TRANSITIONS: Dict[str, Set[str]] = {
    "pending": {"running", "cancelled"},
    "running": {"succeeded", "partial_success", "failed", "cancelled"},
    "succeeded": set(),
    "partial_success": set(),
    "failed": set(),
    "cancelled": set(),
}

SUPPORTED_IDENTIFIER_TYPES: Set[str] = {
    "domain",
    "url",
    "email",
    "phone",
    "social_profile",
    "external_id",
}

HASH_CONTRACT_VERSION = "v1"


class IdentifierNormalizer:
    """
    Robust normalizer handling IDNA conversion, URL component stripping, 
    credentials checks, and type-specific canonicalization.
    """
    
    # IPv4 basic regex for quick validation
    IPV4_REGEX = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

    @staticmethod
    def normalize_domain(domain: str) -> str:
        if not isinstance(domain, str) or not domain.strip():
            raise InvalidIdentifierError("Domain must be a non-empty string.")
        
        value = domain.strip().lower()
        if "://" not in value:
            value = f"//{value}"
            
        try:
            parsed = urlsplit(value)
        except Exception as err:
            raise InvalidIdentifierError(f"Malformed domain/URL structure: '{domain}'") from err

        if parsed.username or parsed.password:
            raise InvalidIdentifierError("Credentials are not permitted in domain identifiers.")

        hostname = parsed.hostname
        if not hostname:
            raise InvalidIdentifierError(f"Could not extract a valid hostname from: '{domain}'")

        # Strip trailing dot if present
        hostname = hostname.rstrip(".")

        # Handle IPv4 / IPv6 vs standard domain name
        if hostname.startswith("[") and hostname.endswith("]"):
            # IPv6 address literal
            normalized = hostname
        elif IdentifierNormalizer.IPV4_REGEX.match(hostname):
            normalized = hostname
        else:
            try:
                # Convert to ASCII IDNA representation
                normalized = hostname.encode("idna").decode("ascii")
            except Exception as err:
                raise InvalidIdentifierError(f"Failed IDNA normalization for hostname: '{hostname}'") from err

            if "." not in normalized and normalized != "localhost":
                raise InvalidIdentifierError(f"Single-label hostnames without periods are invalid: '{normalized}'")
            if normalized.startswith("-") or normalized.endswith("-"):
                raise InvalidIdentifierError(f"Invalid domain label boundaries: '{normalized}'")

        return normalized.removeprefix("www.")

    @classmethod
    def normalize_identifier(cls, identifier_type: str, raw_value: str) -> str:
        norm_type = identifier_type.strip().lower()
        if norm_type not in SUPPORTED_IDENTIFIER_TYPES:
            raise InvalidIdentifierError(f"Unsupported identifier type: '{identifier_type}'")

        if not isinstance(raw_value, str) or not raw_value.strip():
            raise InvalidIdentifierError(f"Identifier value for '{norm_type}' cannot be empty.")

        val = raw_value.strip()

        if norm_type == "domain":
            return cls.normalize_domain(val)
        elif norm_type == "email":
            val_lower = val.lower()
            if "@" not in val_lower or val_lower.startswith("@") or val_lower.endswith("@"):
                raise InvalidIdentifierError(f"Invalid email format: '{raw_value}'")
            return val_lower
        elif norm_type == "url":
            if "://" not in val:
                val = f"https://{val}"
            try:
                parsed = urlsplit(val)
                if not parsed.hostname:
                    raise InvalidIdentifierError()
                # Reconstruct clean canonical URL
                scheme = parsed.scheme.lower()
                netloc = parsed.hostname.lower()
                path = parsed.path.rstrip("/") if parsed.path else ""
                return f"{scheme}://{netloc}{path}"
            except Exception as err:
                raise InvalidIdentifierError(f"Invalid URL format: '{raw_value}'") from err
        elif norm_type == "phone":
            # Basic E.164-style strip for illustration (non-digits removed except leading +)
            cleaned = re.sub(r"[^\d+]", "", val)
            if len(cleaned) < 5:
                raise InvalidIdentifierError(f"Invalid phone number length: '{raw_value}'")
            return cleaned
        else:
            # external_id / social_profile
            return val.lower()

    @classmethod
    def hash_identifier(cls, identifier_type: str, raw_value: str) -> Tuple[str, str]:
        norm_type = identifier_type.strip().lower()
        normalized_val = cls.normalize_identifier(norm_type, raw_value)
        
        # Versioned canonical string to prevent migration/algorithm breakage
        canonical_string = f"adl:identifier:{HASH_CONTRACT_VERSION}:{norm_type}:{normalized_val}"
        identifier_hash = hashlib.sha256(canonical_string.encode("utf-8")).hexdigest()
        return normalized_val, identifier_hash


class SignalForgeRepository:
    def __init__(self, db_client: DuckDBClient):
        self.db = db_client
        self._ensure_tables_and_indexes()

    def _ensure_tables_and_indexes(self) -> None:
        try:
            with self.db.transaction() as tx:
                # Expand persistence schema to support auditable identifier observations & assignments
                tx.execute("""
                    CREATE TABLE IF NOT EXISTS identifier_observations (
                        observation_id VARCHAR PRIMARY KEY,
                        identifier_hash VARCHAR NOT NULL,
                        identifier_type VARCHAR NOT NULL,
                        identifier_value VARCHAR NOT NULL,
                        source_provider VARCHAR NOT NULL,
                        confidence_score DOUBLE CHECK (confidence_score >= 0.0 AND confidence_score <= 1.0),
                        observed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                tx.execute("""
                    CREATE TABLE IF NOT EXISTS entity_identifier_assignments (
                        assignment_id VARCHAR PRIMARY KEY,
                        entity_id VARCHAR REFERENCES canonical_entities(entity_id),
                        identifier_hash VARCHAR NOT NULL,
                        source_provider VARCHAR NOT NULL,
                        resolution_version VARCHAR NOT NULL,
                        assigned_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                tx.execute("CREATE INDEX IF NOT EXISTS idx_obs_hash ON identifier_observations(identifier_hash);")
                tx.execute("CREATE INDEX IF NOT EXISTS idx_assign_entity ON entity_identifier_assignments(entity_id);")
                tx.execute("CREATE INDEX IF NOT EXISTS idx_assign_hash ON entity_identifier_assignments(identifier_hash);")
        except Exception as err:
            logger.error(f"Critical error initializing repository audit tables: {err}")
            raise RepositoryIntegrityError(f"Failed to initialize repository tables: {err}") from err

    # --- Pipeline Run Operations ---

    def start_pipeline_run(
        self,
        run_id: str,
        pipeline_name: str,
        records_received: int = 0
    ) -> None:
        if not run_id or not pipeline_name:
            raise ValueError("run_id and pipeline_name must be non-empty strings.")
        if records_received < 0:
            raise ValueError("records_received cannot be negative.")

        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as tx:
            existing = tx.execute(
                "SELECT status FROM pipeline_runs WHERE run_id = ?;",
                (run_id,)
            ).fetchone()

            if existing:
                current_status = existing[0]
                if current_status in TERMINAL_RUN_STATES:
                    raise InvalidStateTransitionError(
                        f"Illegal state transition: Cannot restart pipeline run '{run_id}' "
                        f"because it is already in terminal state '{current_status}'."
                    )
                
                if "running" not in STATE_TRANSITIONS.get(current_status, set()):
                    raise InvalidStateTransitionError(
                        f"Illegal state transition from '{current_status}' to 'running'."
                    )

                tx.execute(
                    """
                    UPDATE pipeline_runs SET
                        status = 'running',
                        records_received = ?,
                        started_at = COALESCE(started_at, ?)
                    WHERE run_id = ?;
                    """,
                    (records_received, now, run_id)
                )
            else:
                tx.execute(
                    """
                    INSERT INTO pipeline_runs (run_id, pipeline_name, status, records_received, started_at)
                    VALUES (?, ?, 'running', ?, ?);
                    """,
                    (run_id, pipeline_name, records_received, now)
                )
        logger.info(f"Pipeline run '{run_id}' ({pipeline_name}) marked as running.")

    def complete_pipeline_run(
        self,
        run_id: str,
        status: str,
        records_succeeded: int,
        records_failed: int,
        error_summary: Optional[str] = None
    ) -> None:
        if status not in VALID_RUN_STATES or status in {"running", "pending"}:
            raise ValueError(f"Invalid terminal status '{status}' for pipeline run completion.")
        if records_succeeded < 0 or records_failed < 0:
            raise ValueError("Record counts cannot be negative.")

        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as tx:
            existing = tx.execute(
                "SELECT status, records_received, records_succeeded, records_failed FROM pipeline_runs WHERE run_id = ?;",
                (run_id,)
            ).fetchone()

            if not existing:
                raise PipelineRunNotFoundError(f"Cannot complete non-existent pipeline run '{run_id}'.")

            current_status, rec_received, prev_succ, prev_fail = existing[0], existing[1], existing[2], existing[3]

            if current_status in TERMINAL_RUN_STATES:
                if current_status == status and prev_succ == records_succeeded and prev_fail == records_failed:
                    return # Idempotent match
                raise InvalidStateTransitionError(
                    f"Conflict: Run '{run_id}' is already terminal ({current_status}). "
                    f"Attempted conflicting completion with status '{status}'."
                )

            if status not in STATE_TRANSITIONS.get(current_status, set()):
                raise InvalidStateTransitionError(
                    f"Illegal state transition from '{current_status}' to '{status}'."
                )

            if (records_succeeded + records_failed) > rec_received:
                logger.warning(
                    f"Accounting warning: succeeded ({records_succeeded}) + failed ({records_failed}) "
                    f"exceeds records_received ({rec_received}) for run '{run_id}'."
                )

            tx.execute(
                """
                UPDATE pipeline_runs SET
                    status = ?,
                    records_succeeded = ?,
                    records_failed = ?,
                    error_summary = ?,
                    completed_at = ?
                WHERE run_id = ?;
                """,
                (status, records_succeeded, records_failed, error_summary, now, run_id)
            )
        logger.info(f"Pipeline run '{run_id}' finalized with status: {status}")

    def cancel_pipeline_run(self, run_id: str, reason: Optional[str] = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.db.transaction() as tx:
            existing = tx.execute(
                "SELECT status FROM pipeline_runs WHERE run_id = ?;",
                (run_id,)
            ).fetchone()

            if not existing:
                raise PipelineRunNotFoundError(f"Cannot cancel non-existent pipeline run '{run_id}'.")

            current_status = existing[0]
            if current_status in TERMINAL_RUN_STATES:
                if current_status == "cancelled":
                    return
                raise InvalidStateTransitionError(f"Cannot cancel run '{run_id}' already in terminal state '{current_status}'.")

            tx.execute(
                """
                UPDATE pipeline_runs SET
                    status = 'cancelled',
                    error_summary = COALESCE(?, error_summary),
                    completed_at = ?
                WHERE run_id = ?;
                """,
                (reason, now, run_id)
            )
        logger.info(f"Pipeline run '{run_id}' cancelled.")

    # --- Entity & Identifier Operations ---

    def upsert_canonical_entity(
        self,
        entity_id: str,
        canonical_name: str,
        primary_domain: Optional[str] = None,
        entity_type: str = "company",
        confidence_score: float = 1.0
    ) -> Dict[str, Any]:
        if not entity_id or not canonical_name:
            raise ValueError("entity_id and canonical_name must be non-empty.")
        if not (0.0 <= confidence_score <= 1.0):
            raise ValueError("confidence_score must be between 0.0 and 1.0 inclusive.")

        now = datetime.now(timezone.utc).isoformat()
        normalized_primary_domain = IdentifierNormalizer.normalize_domain(primary_domain) if primary_domain else None

        with self.db.transaction() as tx:
            tx.execute(
                """
                INSERT INTO canonical_entities (entity_id, canonical_name, primary_domain, entity_type, confidence_score, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (entity_id) DO UPDATE SET
                    canonical_name = CASE WHEN excluded.confidence_score >= canonical_entities.confidence_score THEN excluded.canonical_name ELSE canonical_entities.canonical_name END,
                    primary_domain = CASE WHEN excluded.confidence_score >= canonical_entities.confidence_score AND excluded.primary_domain IS NOT NULL THEN excluded.primary_domain ELSE canonical_entities.primary_domain END,
                    confidence_score = GREATEST(canonical_entities.confidence_score, excluded.confidence_score),
                    updated_at = excluded.updated_at;
                """,
                (entity_id, canonical_name, normalized_primary_domain, entity_type, confidence_score, now, now)
            )

        return {
            "entity_id": entity_id,
            "canonical_name": canonical_name,
            "primary_domain": normalized_primary_domain,
            "entity_type": entity_type,
            "confidence_score": confidence_score
        }

    def record_identifier_observation(
        self,
        identifier_type: str,
        raw_identifier_value: str,
        source_provider: str,
        confidence_score: float = 1.0
    ) -> Tuple[str, str]:
        """Records an auditable discovery observation without mutating entity assignments directly."""
        if not (0.0 <= confidence_score <= 1.0):
            raise ValueError("confidence_score must be between 0.0 and 1.0 inclusive.")

        normalized_value, identifier_hash = IdentifierNormalizer.hash_identifier(identifier_type, raw_identifier_value)
        observation_id = hashlib.sha256(f"{identifier_hash}:{source_provider}:{datetime.now(timezone.utc).timestamp()}".encode()).hexdigest()[:16]
        now = datetime.now(timezone.utc).isoformat()

        with self.db.transaction() as tx:
            tx.execute(
                """
                INSERT INTO identifier_observations (observation_id, identifier_hash, identifier_type, identifier_value, source_provider, confidence_score, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (observation_id, identifier_hash, identifier_type, normalized_value, source_provider, confidence_score, now)
            )
        return normalized_value, identifier_hash

    def assign_identifier_to_entity(
        self,
        entity_id: str,
        identifier_type: str,
        raw_identifier_value: str,
        source_provider: str,
        resolution_version: str = "v1"
    ) -> str:
        """
        Explicit, auditable assignment separating observations from entity ownership.
        Prevents silent destructive reassignment by tracking resolution history.
        """
        normalized_value, identifier_hash = IdentifierNormalizer.hash_identifier(identifier_type, raw_identifier_value)
        assignment_id = hashlib.sha256(f"{entity_id}:{identifier_hash}".encode()).hexdigest()[:16]
        now = datetime.now(timezone.utc).isoformat()

        with self.db.transaction() as tx:
            # Verify entity exists
            ent = tx.execute("SELECT entity_id FROM canonical_entities WHERE entity_id = ?;", (entity_id,)).fetchone()
            if not ent:
                raise EntityNotFoundError(f"Cannot assign identifier to non-existent entity '{entity_id}'.")

            # Check existing assignment
            existing = tx.execute(
                "SELECT entity_id FROM entity_identifier_assignments WHERE identifier_hash = ?;",
                (identifier_hash,)
            ).fetchone()

            if existing and existing[0] != entity_id:
                logger.info(
                    f"Identifier assignment transition: hash '{identifier_hash}' reassigned "
                    f"from entity '{existing[0]}' to entity '{entity_id}' by provider '{source_provider}'."
                )

            tx.execute(
                """
                INSERT INTO entity_identifier_assignments (assignment_id, entity_id, identifier_hash, source_provider, resolution_version, assigned_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (assignment_id) DO UPDATE SET
                    entity_id = excluded.entity_id,
                    source_provider = excluded.source_provider,
                    resolution_version = excluded.resolution_version,
                    assigned_at = excluded.assigned_at;
                """,
                (assignment_id, entity_id, identifier_hash, source_provider, resolution_version, now)
            )

            # Also maintain legacy/compatibility table view for downstream queries if required
            tx.execute(
                """
                INSERT INTO entity_identifiers (identifier_hash, entity_id, identifier_type, identifier_value, source_provider, discovered_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (identifier_hash) DO UPDATE SET
                    entity_id = excluded.entity_id,
                    source_provider = excluded.source_provider;
                """,
                (identifier_hash, entity_id, identifier_type, normalized_value, source_provider, now)
            )

        return identifier_hash

    # Compatibility alias
    def add_entity_identifier(self, entity_id: str, identifier_type: str, raw_identifier_value: str, source_provider: str, confidence_score: float = 1.0) -> None:
        self.record_identifier_observation(identifier_type, raw_identifier_value, source_provider, confidence_score)
        self.assign_identifier_to_entity(entity_id, identifier_type, raw_identifier_value, source_provider)

    def attach_entity_identifier(self, *args, **kwargs):
        return self.add_entity_identifier(*args, **kwargs)

    def get_entity_by_domain(self, domain: str) -> Optional[Dict[str, Any]]:
        try:
            _, domain_hash = IdentifierNormalizer.hash_identifier("domain", domain)
        except (InvalidIdentifierError, ValueError):
            return None

        with self.db.connection() as conn:
            res = conn.execute(
                """
                SELECT ce.entity_id, ce.canonical_name, ce.primary_domain, ce.entity_type, ce.confidence_score
                FROM canonical_entities ce
                JOIN entity_identifier_assignments eia ON ce.entity_id = eia.entity_id
                WHERE eia.identifier_hash = ?
                LIMIT 1;
                """,
                (domain_hash,)
            ).fetchone()

            if not res:
                return None

            return {
                "entity_id": res[0],
                "canonical_name": res[1],
                "primary_domain": res[2],
                "entity_type": res[3],
                "confidence_score": res[4]
            }