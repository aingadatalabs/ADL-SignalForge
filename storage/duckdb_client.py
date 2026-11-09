"""Backward-compatible imports for the shared DuckDB database client."""

from core.database import (
    ClientState,
    DATASET_CATALOG,
    DatasetDefinition,
    DuckDBClient,
    OperationLease,
)

__all__ = [
    "ClientState",
    "DATASET_CATALOG",
    "DatasetDefinition",
    "DuckDBClient",
    "OperationLease",
]
