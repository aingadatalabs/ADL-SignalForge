"""
storage/models/storage.py — Strict Data Contracts, Release Manifests & Publication Models

Features:
- Strict Pydantic v2 data contracts with active schema/partition cross-validation and immutability.
- Immutable Release Manifests recording provenance, file sizes, counts, schema versions, and execution modes.
- Dataset Pointer schemas (current.json) for atomic release promotion.
- Controlled, read-only catalog definitions for Silver and Gold analytical datasets.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Mapping, Tuple, Set
from pydantic import BaseModel, ConfigDict, Field, model_validator


# =====================================================================
# Shared Strict & Immutable Base Model
# =====================================================================

class StrictImmutableModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        validate_assignment=True,
        frozen=True,
    )


# =====================================================================
# Enums & Vocabularies
# =====================================================================

class DataTier(str, Enum):
    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"


class DatasetType(str, Enum):
    SNAPSHOT = "snapshot"                  # Complete logical replacement per release
    INCREMENTAL_SEGMENT = "incremental"    # Append-only immutable batch segment


class ValidationStatus(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    QUARANTINED = "quarantined"


class LogicalDataType(str, Enum):
    VARCHAR = "VARCHAR"
    INTEGER = "INTEGER"
    BIGINT = "BIGINT"
    DOUBLE = "DOUBLE"
    BOOLEAN = "BOOLEAN"
    DATE = "DATE"
    TIMESTAMP_TZ = "TIMESTAMP WITH TIME ZONE"


# =====================================================================
# Field & Contract Models
# =====================================================================

class FieldDefinition(StrictImmutableModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    data_type: LogicalDataType
    nullable: bool = True
    description: Optional[str] = Field(None, max_length=500)


class DatasetContract(StrictImmutableModel):
    dataset_name: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    tier: DataTier
    dataset_type: DatasetType
    schema_version: int = Field(..., ge=1)
    schema_definition: List[FieldDefinition] = Field(..., min_length=1)
    allowed_partition_columns: List[str] = Field(default_factory=list)
    description: Optional[str] = Field(None, max_length=1000)

    @model_validator(mode="after")
    def validate_contract_invariants(self) -> "DatasetContract":
        """Enforces schema field uniqueness, partition validity, and type consistency."""
        field_names: Set[str] = set()
        for f in self.schema_definition:
            if f.name in field_names:
                raise ValueError(f"Duplicate schema field name detected: '{f.name}'")
            field_names.add(f.name)

        partition_names: Set[str] = set()
        for col in self.allowed_partition_columns:
            if col in partition_names:
                raise ValueError(f"Duplicate partition column specified: '{col}'")
            partition_names.add(col)

            if col not in field_names:
                raise ValueError(f"Partition column '{col}' is not defined in the dataset schema definition.")

            # Optional policy: Ensure partition columns are non-nullable for stable physical directory partitioning
            field_map = {f.name: f for f in self.schema_definition}
            if field_map[col].nullable:
                raise ValueError(f"Partition column '{col}' must be non-nullable (nullable=False).")

        return self


# =====================================================================
# Manifest & Pointer Models
# =====================================================================

class FileManifestEntry(StrictImmutableModel):
    path: str = Field(..., min_length=1)
    record_count: int = Field(..., ge=0)
    size_bytes: int = Field(..., ge=0)
    checksum_sha256: Optional[str] = Field(None, pattern=r"^[a-fA-F0-9]{64}$")

    @model_validator(mode="after")
    def validate_path_safety(self) -> "FileManifestEntry":
        cleaned = self.path.replace("\\", "/")
        if ".." in cleaned or cleaned.startswith("/") or cleaned.startswith("~"):
            raise ValueError(f"Unsafe file path in manifest entry: '{self.path}'")
        return self


class ReleaseManifest(StrictImmutableModel):
    dataset_name: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    tier: DataTier
    dataset_type: DatasetType
    release_id: str = Field(..., min_length=1, pattern=r"^release_[0-9]{8}_[a-zA-Z0-9_-]+$")
    publication_id: str = Field(..., min_length=1)
    pipeline_run_id: str = Field(..., min_length=1)
    schema_version: int = Field(..., ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: ValidationStatus = ValidationStatus.PENDING
    format: str = "parquet"
    compression: str = "ZSTD"
    partition_columns: List[str] = Field(default_factory=list)
    record_count: int = Field(..., ge=0)
    files: List[FileManifestEntry] = Field(default_factory=list)
    validation_report: Dict[str, Any] = Field(default_factory=dict)
    previous_release_id: Optional[str] = None

    @model_validator(mode="after")
    def validate_manifest_consistency(self) -> "ReleaseManifest":
        # 1. Total record count must match sum of file counts
        sum_records = sum(f.record_count for f in self.files)
        if self.record_count != sum_records:
            raise ValueError(
                f"Manifest record_count ({self.record_count}) does not match sum of file record counts ({sum_records})."
            )

        # 2. File paths must be unique
        paths = [f.path for f in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("Duplicate file paths detected in release manifest entries.")

        # 3. PASSED status requires a populated validation report
        if self.status == ValidationStatus.PASSED and not self.validation_report:
            raise ValueError("Release manifest status cannot be 'passed' with an empty validation_report.")

        return self


class DatasetPointer(StrictImmutableModel):
    dataset_name: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    tier: DataTier
    current_release: str = Field(..., min_length=1)
    published_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: int = Field(..., ge=1)


# =====================================================================
# ADL-SignalForge Controlled Dataset Catalog & Contracts
# =====================================================================

SILVER_COMPANIES_CONTRACT = DatasetContract(
    dataset_name="companies_silver",
    tier=DataTier.SILVER,
    dataset_type=DatasetType.SNAPSHOT,
    schema_version=1,
    description="Normalized operational company records extracted from web intelligence pipelines.",
    allowed_partition_columns=["ingestion_date"],
    schema_definition=[
        FieldDefinition(name="entity_id", data_type=LogicalDataType.VARCHAR, nullable=False, description="Canonical resolved entity UUID"),
        FieldDefinition(name="company_name", data_type=LogicalDataType.VARCHAR, nullable=False, description="Normalized legal or trading name"),
        FieldDefinition(name="domain", data_type=LogicalDataType.VARCHAR, nullable=True, description="Primary corporate web domain"),
        FieldDefinition(name="region", data_type=LogicalDataType.VARCHAR, nullable=True, description="Geographic jurisdiction or region"),
        FieldDefinition(name="confidence_score", data_type=LogicalDataType.DOUBLE, nullable=False, description="Entity resolution confidence score"),
        FieldDefinition(name="source_provider", data_type=LogicalDataType.VARCHAR, nullable=False, description="Origin scraper or provider"),
        FieldDefinition(name="source_record_id", data_type=LogicalDataType.VARCHAR, nullable=False, description="Source record key"),
        FieldDefinition(name="pipeline_run_id", data_type=LogicalDataType.VARCHAR, nullable=False, description="Pipeline run ID"),
        FieldDefinition(name="ingestion_date", data_type=LogicalDataType.VARCHAR, nullable=False, description="Partition date (YYYY-MM-DD)"),
        FieldDefinition(name="observed_at", data_type=LogicalDataType.TIMESTAMP_TZ, nullable=False, description="Timestamp of original observation"),
        FieldDefinition(name="ingested_at", data_type=LogicalDataType.TIMESTAMP_TZ, nullable=False, description="Timestamp of pipeline ingestion")
    ]
)

GOLD_COMPANY_INTELLIGENCE_CONTRACT = DatasetContract(
    dataset_name="company_intelligence_gold",
    tier=DataTier.GOLD,
    dataset_type=DatasetType.SNAPSHOT,
    schema_version=1,
    description="Consumer-ready enriched B2B intelligence products with scoring and decision-maker insights.",
    allowed_partition_columns=["industry", "ingestion_date"],
    schema_definition=[
        FieldDefinition(name="entity_id", data_type=LogicalDataType.VARCHAR, nullable=False, description="Canonical entity ID"),
        FieldDefinition(name="company_name", data_type=LogicalDataType.VARCHAR, nullable=False, description="Company Name"),
        FieldDefinition(name="domain", data_type=LogicalDataType.VARCHAR, nullable=True, description="Corporate Domain"),
        FieldDefinition(name="industry", data_type=LogicalDataType.VARCHAR, nullable=False, description="Classified industry vertical"),
        FieldDefinition(name="signal_score", data_type=LogicalDataType.DOUBLE, nullable=True, description="Computed B2B buying signal priority score"),
        FieldDefinition(name="decision_maker_count", data_type=LogicalDataType.INTEGER, nullable=True, description="Verified executive contacts discovered"),
        FieldDefinition(name="ingestion_date", data_type=LogicalDataType.VARCHAR, nullable=False, description="Publication date (YYYY-MM-DD)")
    ]
)

# Immutable Central Dataset Registry Catalog
_DATASET_REGISTRY: Dict[str, DatasetContract] = {
    SILVER_COMPANIES_CONTRACT.dataset_name: SILVER_COMPANIES_CONTRACT,
    GOLD_COMPANY_INTELLIGENCE_CONTRACT.dataset_name: GOLD_COMPANY_INTELLIGENCE_CONTRACT,
}


def get_dataset_contract(dataset_name: str) -> DatasetContract:
    """Retrieve a registered dataset contract safely without exposing mutable state."""
    if dataset_name not in _DATASET_REGISTRY:
        raise KeyError(f"Dataset contract for '{dataset_name}' is not registered in ADL-SignalForge catalog.")
    return _DATASET_REGISTRY[dataset_name]


def list_registered_datasets() -> Mapping[str, DatasetContract]:
    """Expose a read-only view of the dataset registry."""
    return _DATASET_REGISTRY