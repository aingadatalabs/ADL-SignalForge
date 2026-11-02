"""
storage/__init__.py — ADL-SignalForge Storage & Publication Subsystem Facade

Exposes core storage engines, durable JSONL stores, Parquet sinks, repositories,
and strict data contracts/publication models for downstream pipeline and API consumption.
"""

# Core Storage & Processing Clients
from storage.duckdb_client import DuckDBClient
from storage.jsonl_store import JSONLStore
from storage.parquet_sink import ParquetSink
from storage.repository import SignalForgeRepository

# Strict Data Contracts, Manifests & Registry
from storage.models.storage import (
    DataTier,
    DatasetType,
    ValidationStatus,
    LogicalDataType,
    FieldDefinition,
    DatasetContract,
    FileManifestEntry,
    ReleaseManifest,
    DatasetPointer,
    SILVER_COMPANIES_CONTRACT,
    GOLD_COMPANY_INTELLIGENCE_CONTRACT,
    get_dataset_contract,
    list_registered_datasets,
)

__all__ = [
    # Engines & Clients
    "DuckDBClient",
    "JSONLStore",
    "ParquetSink",
    "SignalForgeRepository",
    
    # Models & Enums
    "DataTier",
    "DatasetType",
    "ValidationStatus",
    "LogicalDataType",
    "FieldDefinition",
    "DatasetContract",
    "FileManifestEntry",
    "ReleaseManifest",
    "DatasetPointer",
    
    # Pre-registered Catalog & Helpers
    "SILVER_COMPANIES_CONTRACT",
    "GOLD_COMPANY_INTELLIGENCE_CONTRACT",
    "get_dataset_contract",
    "list_registered_datasets",
]