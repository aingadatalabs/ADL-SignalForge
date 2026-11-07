"""

Features:
- Atomic operation leasing to eliminate shutdown/writer race conditions.
- Strict dataset catalog & column whitelisting to eliminate SQL identifier injection.
- Guaranteed resource cleanup & exception-safe reader counter tracking.
- Robust condition-based graceful shutdown with monotonic deadline enforcement.
- Exclusive ACID transaction management with fail-safe rollback state transition.
"""

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Set, Union
import duckdb

logger = logging.getLogger("DuckDBClient")


class ClientState(Enum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


@dataclass(frozen=True)
class DatasetDefinition:
    name: str
    relative_path: str
    allowed_columns: Set[str]


# Controlled Dataset Registry mapping logical names to safe path patterns and column schemas
DATASET_CATALOG: Dict[str, DatasetDefinition] = {
    "silver_companies": DatasetDefinition(
        name="silver_companies",
        relative_path="silver/companies/**/*.parquet",
        allowed_columns={
            "entity_id",
            "company_name",
            "domain",
            "country",
            "confidence_score",
            "created_at",
            "updated_at",
        },
    ),
    "gold_intelligence": DatasetDefinition(
        name="gold_intelligence",
        relative_path="gold/intelligence/**/*.parquet",
        allowed_columns={
            "entity_id",
            "canonical_name",
            "primary_domain",
            "intelligence_payload",
            "confidence_score",
            "created_at",
            "updated_at",
        },
    ),
}


class OperationLease:
    """
    Context manager enforcing atomic lifecycle admission and reliable active counter tracking.
    """

    def __init__(self, client: "DuckDBClient", is_write: bool):
        self.client = client
        self.is_write = is_write

    def __enter__(self):
        with self.client._state_lock:
            if self.client._state != ClientState.OPEN:
                raise RuntimeError(
                    f"Cannot acquire operation lease. Client state is {self.client._state.value}."
                )

            if self.is_write:
                if self.client.read_only:
                    raise RuntimeError(
                        "Cannot execute write operation on a read-only DuckDB client instance."
                    )
                self.client._active_writes += 1
            else:
                self.client._active_readers += 1
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        with self.client._state_lock:
            if self.is_write:
                self.client._active_writes -= 1
            else:
                self.client._active_readers -= 1

            # Notify shutdown waiter when active operations reach zero
            if self.client._active_readers == 0 and self.client._active_writes == 0:
                self.client._drained_condition.notify_all()


class DuckDBClient:
    def __init__(
        self,
        db_path: Union[str, Path] = "./data/adl_signalforge.duckdb",
        read_only: bool = False,
        memory_limit: str = "2GB",
        threads: Optional[int] = None,
        base_data_dir: Union[str, Path] = "./data",
    ):
        """
        Initialize the production DuckDB client wrapper.
        """
        self.db_path = Path(db_path).resolve()
        if not read_only:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.read_only = read_only
        self.memory_limit = memory_limit
        self.threads = threads if threads is not None else 2
        self.base_data_dir = Path(base_data_dir).resolve()

        self._state = ClientState.OPEN
        self._state_lock = threading.Lock()
        self._drained_condition = threading.Condition(self._state_lock)
        self._write_lock = threading.Lock()

        # Operational counters
        self._active_readers = 0
        self._active_writes = 0

        self._write_conn: Optional[duckdb.DuckDBPyConnection] = None

        if not self.read_only:
            self._write_conn = self._connect_raw(read_only=False)
            self._initialize_migration_table()
            self._run_migrations()

        logger.info(
            f"DuckDBClient initialized for database: {self.db_path} (Read-only: {self.read_only})"
        )

    @property
    def state(self) -> ClientState:
        with self._state_lock:
            return self._state

    def _connect_raw(self, read_only: bool) -> duckdb.DuckDBPyConnection:
        """Create and configure a raw DuckDB connection with resource limits."""
        conn = duckdb.connect(database=str(self.db_path), read_only=read_only)
        if self.memory_limit:
            conn.execute(f"SET memory_limit='{self.memory_limit}';")
        if self.threads and self.threads > 0:
            conn.execute(f"SET threads={self.threads};")
        return conn

    @contextmanager
    def connection(self) -> Generator[duckdb.DuckDBPyConnection, None, None]:
        """
        Context manager for safely executing read queries with lifecycle enforcement.
        """
        with OperationLease(self, is_write=False):
            read_conn = self._connect_raw(read_only=True)
            try:
                yield read_conn
            except Exception as err:
                logger.error(f"Error during DuckDB read operation: {err}", exc_info=True)
                raise
            finally:
                read_conn.close()

    def check_connection(self) -> bool:
        """Check database availability using the existing connection configuration."""
        if self.read_only:
            with self.connection() as conn:
                return conn.execute("SELECT 1").fetchone() == (1,)

        with OperationLease(self, is_write=False):
            with self._write_lock:
                if self._write_conn is None:
                    raise RuntimeError("Active write connection is missing.")
                return self._write_conn.execute("SELECT 1").fetchone() == (1,)

    @contextmanager
    def transaction(self) -> Generator[duckdb.DuckDBPyConnection, None, None]:
        """
        ACID Transaction context manager with guaranteed rollback and state transition on error.
        """
        if self.read_only:
            raise RuntimeError("Cannot execute transactions on a read-only DuckDB client instance.")

        with self._write_lock, OperationLease(self, is_write=True):
            conn = self._write_conn
            if not conn:
                raise RuntimeError("Active write connection is missing.")

            try:
                conn.execute("BEGIN TRANSACTION;")
                yield conn
                conn.execute("COMMIT;")
            except Exception as err:
                try:
                    conn.execute("ROLLBACK;")
                except Exception as rb_err:
                    logger.critical(f"CRITICAL: Failed to rollback DuckDB transaction: {rb_err}")
                    with self._state_lock:
                        self._state = ClientState.FAILED
                logger.error(f"Transaction failed and rolled back: {err}", exc_info=True)
                raise

    def _initialize_migration_table(self) -> None:
        """Create the schema migration versioning table."""
        with self.transaction() as tx:
            tx.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    migration_name VARCHAR NOT NULL,
                    applied_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)

    def _run_migrations(self) -> None:
        """Apply foundational database schemas and versioned migrations."""
        with self.transaction() as tx:
            applied = {row[0] for row in tx.execute("SELECT version FROM schema_migrations;").fetchall()}

            if 1 not in applied:
                tx.execute("""
                    CREATE TABLE IF NOT EXISTS canonical_entities (
                        entity_id VARCHAR PRIMARY KEY,
                        canonical_name VARCHAR,
                        primary_domain VARCHAR,
                        entity_type VARCHAR DEFAULT 'company',
                        confidence_score DOUBLE CHECK (confidence_score >= 0.0 AND confidence_score <= 1.0),
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                tx.execute("""
                    CREATE TABLE IF NOT EXISTS entity_identifiers (
                        identifier_hash VARCHAR PRIMARY KEY,
                        entity_id VARCHAR REFERENCES canonical_entities(entity_id),
                        identifier_type VARCHAR NOT NULL,
                        identifier_value VARCHAR NOT NULL,
                        source_provider VARCHAR,
                        discovered_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                tx.execute("""
                    CREATE TABLE IF NOT EXISTS pipeline_runs (
                        run_id VARCHAR PRIMARY KEY,
                        pipeline_name VARCHAR NOT NULL,
                        status VARCHAR DEFAULT 'pending',
                        records_received INTEGER DEFAULT 0,
                        records_succeeded INTEGER DEFAULT 0,
                        records_failed INTEGER DEFAULT 0,
                        error_summary VARCHAR,
                        started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        completed_at TIMESTAMP WITH TIME ZONE
                    );
                """)
                tx.execute(
                    "INSERT INTO schema_migrations (version, migration_name) VALUES (?, ?);",
                    (1, "v001_initial_core_schema"),
                )
                logger.info("Applied migration v001: Initial Core Schema successfully.")

    def query_dataset(
        self,
        dataset_name: str,
        columns: Optional[List[str]] = None,
        filters: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
    ) -> Any:
        """
        Securely query Parquet datasets via a Controlled Dataset Registry (Zero SQL Injection).
        Materializes results into an Arrow Table while the read connection is active.
        """
        if dataset_name not in DATASET_CATALOG:
            raise KeyError(f"Dataset '{dataset_name}' is not registered in the controlled dataset catalog.")

        definition = DATASET_CATALOG[dataset_name]

        # Column selection validation
        if columns:
            for col in columns:
                if col not in definition.allowed_columns:
                    raise ValueError(
                        f"Column '{col}' is not permitted for dataset '{dataset_name}'."
                    )
            select_cols = ", ".join([f'"{c}"' for c in columns])
        else:
            select_cols = "*"

        target_glob = str(self.base_data_dir / definition.relative_path)
        query = f"SELECT {select_cols} FROM read_parquet(?)"
        params: List[Any] = [target_glob]

        # Filter criteria validation & parameterization
        if filters:
            clauses = []
            for col, val in filters.items():
                if col not in definition.allowed_columns:
                    raise ValueError(
                        f"Filter column '{col}' is not permitted for dataset '{dataset_name}'."
                    )
                clauses.append(f'"{col}" = ?')
                params.append(val)
            query += " WHERE " + " AND ".join(clauses)

        if limit is not None:
            if not isinstance(limit, int) or limit < 0:
                raise ValueError("Limit must be a non-negative integer.")
            query += " LIMIT ?"
            params.append(limit)

        with self.connection() as conn:
            relation = conn.execute(query, params)
            return relation.fetch_arrow_table()

    def close(self, timeout: float = 5.0) -> bool:
        """
        Gracefully shut down the client, rejecting new leases and draining active operations.
        """
        with self._state_lock:
            if self._state == ClientState.CLOSED:
                return True
            if self._state == ClientState.CLOSING:
                # Wait for ongoing closure attempt
                deadline = time.monotonic() + timeout
                while self._state == ClientState.CLOSING:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._drained_condition.wait(timeout=remaining)
                return self._state == ClientState.CLOSED

            logger.info("Initiating DuckDBClient graceful shutdown...")
            self._state = ClientState.CLOSING

            deadline = time.monotonic() + timeout
            while self._active_readers > 0 or self._active_writes > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._drained_condition.wait(timeout=remaining)

            operations_drained = (self._active_readers == 0 and self._active_writes == 0)

        # Separate write connection cleanup under lock
        with self._write_lock:
            if self._write_conn:
                try:
                    self._write_conn.close()
                except Exception as err:
                    logger.error(f"Error closing write connection: {err}")
                self._write_conn = None

        with self._state_lock:
            if operations_drained:
                self._state = ClientState.CLOSED
                logger.info("DuckDBClient closed successfully.")
                self._drained_condition.notify_all()
                return True
            else:
                logger.error(
                    f"Shutdown timeout reached with {self._active_readers} active readers "
                    f"and {self._active_writes} active writers remaining."
                )
                self._state = ClientState.FAILED
                self._drained_condition.notify_all()
                return False