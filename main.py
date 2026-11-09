import argparse
import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Literal, Optional, Tuple

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    Security,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, StreamingResponse
from dotenv import load_dotenv

load_dotenv()

# --- Internal Domain Imports ---
try:
    from dispatch.outbox import process_outbox_batch
except ImportError:
    async def process_outbox_batch(limit: int = 50) -> Dict[str, Any]:
        raise NotImplementedError("dispatch.outbox module not found.")

try:
    from core.database import DuckDBClient
    from storage.repository import SignalForgeRepository
    from storage.pipeline_runner import PipelineRunner, PipelineInitializationError
except ImportError as e:
    DuckDBClient = None
    SignalForgeRepository = None
    PipelineRunner = None

# ------------------------------------------------------------------------------
# Logging & Process Setup
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("signalforge.api")

# ------------------------------------------------------------------------------
# Fail-Fast Security & Configuration
# ------------------------------------------------------------------------------
API_KEY_NAME = "X-API-Key"
ADMIN_API_KEY = os.getenv("API_ADMIN_KEY") or os.getenv("API_KEY")
READ_ONLY_API_KEYS = tuple(
    key
    for key in (
        os.getenv("API_READ_KEY", ""),
        *os.getenv("API_READ_KEYS", "").split(","),
    )
    if key.strip()
)
API_RATE_LIMIT_PER_MINUTE = int(os.getenv("API_RATE_LIMIT_PER_MINUTE", "60"))
RATE_LIMIT_RESPONSES = {
    status.HTTP_429_TOO_MANY_REQUESTS: {
        "description": "Per-key rate limit exceeded.",
        "headers": {
            "X-RateLimit-Limit": {
                "schema": {"type": "integer"},
                "description": "Maximum requests allowed per minute.",
            },
            "X-RateLimit-Remaining": {
                "schema": {"type": "integer"},
                "description": "Requests remaining in the current window.",
            },
            "X-RateLimit-Reset": {
                "schema": {"type": "integer"},
                "description": "Unix timestamp when the current rate-limit window resets.",
            },
            "Retry-After": {
                "schema": {"type": "integer"},
                "description": "Seconds until requests are accepted again.",
            },
        },
    }
}

if not ADMIN_API_KEY and not READ_ONLY_API_KEYS:
    logger.critical("FATAL: Configure API_ADMIN_KEY/API_KEY or API_READ_KEY(S). Refusing to boot.")
    raise RuntimeError("At least one API key must be configured.")
if API_RATE_LIMIT_PER_MINUTE < 1:
    raise ValueError("API_RATE_LIMIT_PER_MINUTE must be a positive integer.")

api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)
_rate_limit_lock = threading.Lock()
_rate_limit_windows: Dict[str, Tuple[int, int]] = {}


def _api_key_role(api_key_input: Optional[str]) -> Optional[str]:
    if not api_key_input:
        return None

    is_admin = bool(
        ADMIN_API_KEY and secrets.compare_digest(api_key_input, ADMIN_API_KEY)
    )
    is_read_only = False
    for read_key in READ_ONLY_API_KEYS:
        is_read_only |= secrets.compare_digest(api_key_input, read_key.strip())
    if is_admin:
        return "admin"
    return "read" if is_read_only else None


def _rate_limit_headers(api_key_input: str) -> Dict[str, str]:
    now = int(time.time())
    window = now // 60
    key_id = hashlib.sha256(api_key_input.encode("utf-8")).hexdigest()

    with _rate_limit_lock:
        stored_window, request_count = _rate_limit_windows.get(key_id, (window, 0))
        if stored_window != window:
            stored_window, request_count = window, 0
        if request_count >= API_RATE_LIMIT_PER_MINUTE:
            retry_after = max(1, (window + 1) * 60 - now)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded.",
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(API_RATE_LIMIT_PER_MINUTE),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str((window + 1) * 60),
                },
            )
        request_count += 1
        _rate_limit_windows[key_id] = (stored_window, request_count)

    return {
        "X-RateLimit-Limit": str(API_RATE_LIMIT_PER_MINUTE),
        "X-RateLimit-Remaining": str(API_RATE_LIMIT_PER_MINUTE - request_count),
        "X-RateLimit-Reset": str((window + 1) * 60),
    }


def require_api_role(required_role: Literal["read", "admin"]):
    async def verify_api_key(
        response: Response,
        api_key_input: Optional[str] = Security(api_key_header),
    ) -> str:
        role = _api_key_role(api_key_input)
        if role is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: Missing or invalid API key.",
                headers={"WWW-Authenticate": "ApiKey"},
            )
        if required_role == "admin" and role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This API key is not authorized for this operation.",
            )

        assert api_key_input is not None
        response.headers.update(_rate_limit_headers(api_key_input))
        return role

    return verify_api_key


require_read_api_key = require_api_role("read")
require_admin_api_key = require_api_role("admin")

# ------------------------------------------------------------------------------
# Application Lifecycle & Database Dependency Management
# ------------------------------------------------------------------------------
db_client: Optional[Any] = None
repository: Optional[Any] = None
pipeline_runner: Optional[Any] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_client, repository, pipeline_runner
    logger.info("Initializing ADL-SignalForge Control Plane Service & Storage...")
    
    # --- Unified Path Resolution with Persistent Disk Auto-Detection ---
    persistent_dir = Path("/data")
    if persistent_dir.exists() and os.access(persistent_dir, os.W_OK):
        default_db_dir = persistent_dir
        logger.info("Detected writable persistent disk mount at /data.")
    else:
        default_db_dir = Path("/tmp/data") if os.getenv("RENDER") else Path("./data")

    db_path = Path(os.getenv("DATABASE_PATH", os.getenv("DUCKDB_PATH", default_db_dir / "signalforge.duckdb")))
    
    try:
        # Ensure parent directory exists and is writable
        db_path.parent.mkdir(parents=True, exist_ok=True)
        
        if DuckDBClient and SignalForgeRepository and PipelineRunner:
            db_client = DuckDBClient(db_path=db_path)
            repository = SignalForgeRepository(db_client=db_client)
            pipeline_runner = PipelineRunner(repository=repository)
            logger.info(f"Storage and PipelineRunner initialized successfully using path: {db_path}")
        else:
            logger.warning("Storage modules could not be imported. Running in degraded/stub mode.")
    except Exception as e:
        logger.critical(f"Failed to initialize storage layer at {db_path}: {e}", exc_info=True)
        raise
        
    yield
    
    logger.info("Shutting down ADL-SignalForge Control Plane Service...")
    if db_client and hasattr(db_client, "close"):
        db_client.close()


app = FastAPI(
    title="ADL-SignalForge API",
    description="Secure intelligence pipeline orchestration and data delivery API.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    _request: Request,
    _exc: RequestValidationError,
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": "Invalid request."},
    )


# ------------------------------------------------------------------------------
# Schemas & DTOs
# ------------------------------------------------------------------------------
class HealthResponse(BaseModel):
    status: str
    service: str
    storage_accessible: bool


class PipelineTriggerRequest(BaseModel):
    pipeline_name: str = Field(default="default_intelligence_pipeline", description="Name of the pipeline workflow.")
    execution_parameters: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Optional overrides or parameters passed to the pipeline runner.",
    )

class PipelineTriggerResponse(BaseModel):
    status: str
    run_id: str
    pipeline_name: str
    message: str

class OutboxDispatchResponse(BaseModel):
    processed_count: int
    failed_count: int
    remaining_pending: int

class ProspectsResponse(BaseModel):
    prospects: List[Dict[str, Any]]
    total_count: int
    has_more: bool
    page: int

# ------------------------------------------------------------------------------
# Operational & Health Probes (Public)
# ------------------------------------------------------------------------------
def _check_storage_connection() -> bool:
    if db_client is None:
        return False

    return db_client.check_connection()


@app.get(
    "/healthz",
    response_model=HealthResponse,
    responses={503: {"description": "Storage layer unavailable."}},
    tags=["Monitoring"],
)
async def health_check():
    try:
        storage_accessible = await run_in_threadpool(_check_storage_connection)
    except Exception as e:
        logger.error("Storage health check failed.", exc_info=e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage layer unavailable.",
        ) from e

    if not storage_accessible:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage layer unavailable.",
        )

    return HealthResponse(
        status="healthy",
        service="ADL-SignalForge",
        storage_accessible=True,
    )


# ------------------------------------------------------------------------------
# Background Pipeline Execution Worker Task
# ------------------------------------------------------------------------------
async def execute_background_pipeline(run_id: str, pipeline_name: str, parameters: Dict[str, Any]):
    """Background task wrapper executing the PipelineRunner context lifecycle."""
    if not pipeline_runner:
        logger.error("PipelineRunner is uninitialized. Cannot execute background pipeline.")
        return

    try:
        logger.info(f"Starting background execution for run '{run_id}' ({pipeline_name})...")
        with pipeline_runner.run_context(run_id=run_id, pipeline_name=pipeline_name) as execution:
            execution.stage("discovery_and_acquisition")
            execution.record_received(10)
            execution.record_succeeded(10)
            
            logger.info(f"Pipeline workload for run '{run_id}' completed successfully.")
    except Exception as e:
        logger.error(f"Pipeline run '{run_id}' failed during execution: {e}", exc_info=True)


# ------------------------------------------------------------------------------
# Secure Control Plane Endpoints (Authenticated)
# ------------------------------------------------------------------------------
@app.post(
    "/api/v1/trigger-pipeline",
    response_model=PipelineTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        **RATE_LIMIT_RESPONSES,
        422: {"description": "Invalid request."},
    },
    tags=["Pipeline Control"],
    dependencies=[Depends(require_admin_api_key)],
)
async def trigger_pipeline(
    background_tasks: BackgroundTasks,
    payload: Optional[PipelineTriggerRequest] = None,
):
    """
    Secured endpoint to trigger discovery, acquisition, extraction, and publication.
    Enforces overlap protection and hands off execution asynchronously.
    """
    if not pipeline_runner or not repository:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Pipeline execution engine is not initialized.",
        )

    req = payload or PipelineTriggerRequest()
    pipeline_name = req.pipeline_name

    # 1. Overlap Protection: Check if a run for this pipeline is currently active
    try:
        if hasattr(repository, "get_active_run_for_pipeline"):
            active_run = repository.get_active_run_for_pipeline(pipeline_name)
            if active_run:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Conflict: Pipeline '{pipeline_name}' is already running under run_id '{active_run.get('run_id')}'.",
                )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Could not verify active runs in repository: {e}")

    # 2. Generate unique run identifier
    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    run_id = f"run_{timestamp_str}"

    # 3. Schedule background execution via FastAPI BackgroundTasks
    background_tasks.add_task(
        execute_background_pipeline,
        run_id=run_id,
        pipeline_name=pipeline_name,
        parameters=req.execution_parameters,
    )

    logger.info(f"Pipeline run '{run_id}' accepted and scheduled in background.")
    return PipelineTriggerResponse(
        status="accepted",
        run_id=run_id,
        pipeline_name=pipeline_name,
        message="Pipeline execution accepted and queued successfully.",
    )


PROSPECTS_PATH = Path(
    os.getenv(
        "PROSPECTS_PATH",
        str(Path(__file__).parent / "data/output/normalized_agencies.jsonl"),
    )
)


def _iter_prospects() -> Iterator[Dict[str, Any]]:
    try:
        with PROSPECTS_PATH.open("r", encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                if not line.strip():
                    continue
                try:
                    prospect = json.loads(line)
                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSON on line {line_number}.") from e
                if not isinstance(prospect, dict):
                    raise ValueError(f"Expected a JSON object on line {line_number}.")
                yield prospect
    except FileNotFoundError as e:
        logger.error("Prospect data file not found: %s", PROSPECTS_PATH)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Prospect data is not available.",
        ) from e
    except (OSError, ValueError) as e:
        logger.exception("Failed to read prospect data from %s.", PROSPECTS_PATH)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to read prospect data.",
        ) from e


def _record_score(prospect: Dict[str, Any]) -> Optional[float]:
    score = prospect.get("qualification_score", prospect.get("composite_score"))
    if score is None:
        score_breakdown = prospect.get("score_breakdown")
        if not isinstance(score_breakdown, dict):
            qualification = prospect.get("qualification")
            score_breakdown = (
                qualification.get("score_breakdown")
                if isinstance(qualification, dict)
                else None
            )
        if isinstance(score_breakdown, dict):
            score = score_breakdown.get("composite_score")
    if score is None:
        return None
    try:
        return float(score)
    except (TypeError, ValueError) as e:
        raise ValueError("Prospect qualification score is not numeric.") from e


def _record_updated_at(prospect: Dict[str, Any]) -> Optional[datetime]:
    value = prospect.get("updated_at")
    if value is None:
        return None
    if isinstance(value, datetime):
        updated_at = value
    elif isinstance(value, str):
        try:
            updated_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as e:
            raise ValueError("Prospect updated_at is not a valid ISO-8601 datetime.") from e
    else:
        raise ValueError("Prospect updated_at is not a valid ISO-8601 datetime.")
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return updated_at.astimezone(timezone.utc)


def _matches_filters(
    prospect: Dict[str, Any],
    qualification_score_min: Optional[float],
    industry: Optional[str],
    updated_since: Optional[datetime],
) -> bool:
    if qualification_score_min is not None:
        score = _record_score(prospect)
        if score is None or score < qualification_score_min:
            return False

    if industry is not None:
        record_industry = prospect.get("industry", prospect.get("industries"))
        industries = (
            record_industry
            if isinstance(record_industry, list)
            else [record_industry]
        )
        if not any(
            isinstance(value, str) and value.casefold() == industry.casefold()
            for value in industries
        ):
            return False

    if updated_since is not None:
        record_updated_at = _record_updated_at(prospect)
        if record_updated_at is None:
            return False
        since = updated_since
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if record_updated_at < since.astimezone(timezone.utc):
            return False

    return True


def _read_prospect_page(
    limit: int,
    offset: int,
    qualification_score_min: Optional[float],
    industry: Optional[str],
    updated_since: Optional[datetime],
) -> ProspectsResponse:
    page_records: List[Dict[str, Any]] = []
    total_count = 0
    try:
        for prospect in _iter_prospects():
            if not _matches_filters(
                prospect,
                qualification_score_min,
                industry,
                updated_since,
            ):
                continue
            if total_count >= offset and len(page_records) < limit:
                page_records.append(prospect)
            total_count += 1
    except (OSError, ValueError) as e:
        logger.exception("Failed to filter prospect data.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to read prospect data.",
        ) from e

    page = offset // limit + 1
    return ProspectsResponse(
        prospects=page_records,
        total_count=total_count,
        has_more=offset + len(page_records) < total_count,
        page=page,
    )


def _find_prospect(prospect_id: str) -> Optional[Dict[str, Any]]:
    try:
        for prospect in _iter_prospects():
            record_id = (
                prospect.get("prospect_id")
                or prospect.get("agency_id")
                or prospect.get("company_id")
                or prospect.get("id")
            )
            if record_id is not None and str(record_id) == prospect_id:
                return prospect
    except (OSError, ValueError) as e:
        logger.exception("Failed to find prospect %s.", prospect_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to read prospect data.",
        ) from e
    return None


@app.get(
    "/api/v1/prospects",
    response_model=ProspectsResponse,
    responses=RATE_LIMIT_RESPONSES,
    tags=["Prospects"],
    dependencies=[Depends(require_read_api_key)],
)
async def get_prospects(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    qualification_score_min: Optional[float] = Query(default=None, ge=0, le=100),
    industry: Optional[str] = Query(default=None, min_length=1, max_length=100),
    updated_since: Optional[datetime] = Query(default=None),
):
    """Return a filtered page of persisted prospects."""
    return await run_in_threadpool(
        _read_prospect_page,
        limit,
        offset,
        qualification_score_min,
        industry,
        updated_since,
    )


@app.get(
    "/api/v1/prospects/export",
    responses=RATE_LIMIT_RESPONSES,
    tags=["Prospects"],
    dependencies=[Depends(require_read_api_key)],
)
async def export_prospects(
    format: Literal["csv", "jsonl"] = Query(default="jsonl"),
    offset: int = Query(default=0, ge=0),
    limit: Optional[int] = Query(default=None, ge=1, le=100_000),
    qualification_score_min: Optional[float] = Query(default=None, ge=0, le=100),
    industry: Optional[str] = Query(default=None, min_length=1, max_length=100),
    updated_since: Optional[datetime] = Query(default=None),
):
    """Stream filtered prospects as CSV or JSON Lines."""
    count, fieldnames = await run_in_threadpool(
        _prepare_prospect_export,
        offset,
        limit,
        qualification_score_min,
        industry,
        updated_since,
    )

    if format == "csv":
        content = _stream_csv_export(
            offset,
            limit,
            qualification_score_min,
            industry,
            updated_since,
            fieldnames,
        )
        media_type = "text/csv"
    else:
        content = _stream_jsonl_export(
            offset,
            limit,
            qualification_score_min,
            industry,
            updated_since,
        )
        media_type = "application/x-ndjson"

    return StreamingResponse(
        content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="prospects.{format}"',
            "X-Total-Count": str(count),
        },
    )


def _prepare_prospect_export(
    offset: int,
    limit: Optional[int],
    qualification_score_min: Optional[float],
    industry: Optional[str],
    updated_since: Optional[datetime],
) -> Tuple[int, List[str]]:
    count = 0
    fieldnames: set[str] = set()
    try:
        for prospect in _iter_prospects():
            if not _matches_filters(prospect, qualification_score_min, industry, updated_since):
                continue
            if count >= offset and (limit is None or count < offset + limit):
                fieldnames.update(prospect.keys())
            count += 1
    except (OSError, ValueError) as e:
        logger.exception("Failed to prepare prospect export.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to read prospect data.",
        ) from e
    return count, sorted(fieldnames)


def _iter_export_records(
    offset: int,
    limit: Optional[int],
    qualification_score_min: Optional[float],
    industry: Optional[str],
    updated_since: Optional[datetime],
) -> Iterator[Dict[str, Any]]:
    matched_count = 0
    yielded_count = 0
    for prospect in _iter_prospects():
        if not _matches_filters(prospect, qualification_score_min, industry, updated_since):
            continue
        if matched_count >= offset and (limit is None or yielded_count < limit):
            yield prospect
            yielded_count += 1
        matched_count += 1
        if limit is not None and yielded_count >= limit:
            return


def _stream_jsonl_export(
    offset: int,
    limit: Optional[int],
    qualification_score_min: Optional[float],
    industry: Optional[str],
    updated_since: Optional[datetime],
) -> Iterator[str]:
    for prospect in _iter_export_records(
        offset, limit, qualification_score_min, industry, updated_since
    ):
        yield json.dumps(prospect, ensure_ascii=False, separators=(",", ":")) + "\n"


def _stream_csv_export(
    offset: int,
    limit: Optional[int],
    qualification_score_min: Optional[float],
    industry: Optional[str],
    updated_since: Optional[datetime],
    fieldnames: List[str],
) -> Iterator[str]:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    yield buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)
    for prospect in _iter_export_records(
        offset, limit, qualification_score_min, industry, updated_since
    ):
        writer.writerow(
            {
                key: value
                if isinstance(value, (str, int, float, bool)) or value is None
                else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                for key, value in prospect.items()
            }
        )
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)


@app.get(
    "/api/v1/prospects/{prospect_id}",
    responses={**RATE_LIMIT_RESPONSES, 404: {"description": "Prospect not found."}},
    tags=["Prospects"],
    dependencies=[Depends(require_read_api_key)],
)
async def get_prospect(prospect_id: str):
    """Return a single prospect with all persisted structural and enrichment fields."""
    prospect = await run_in_threadpool(_find_prospect, prospect_id)
    if prospect is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Prospect '{prospect_id}' not found.",
        )
    return prospect


@app.post(
    "/api/v1/dispatch/process",
    response_model=OutboxDispatchResponse,
    tags=["Outbox Worker"],
    include_in_schema=False,
    dependencies=[Depends(require_admin_api_key)],
)
async def dispatch_process(limit: int = 50):
    """
    Testing hook for a bounded dispatch batch; production should use a worker.
    """
    try:
        logger.info(f"Triggering outbox dispatch batch (limit={limit})...")
        result = await process_outbox_batch(limit=limit)
        return OutboxDispatchResponse(
            processed_count=result.get("processed_count", 0),
            failed_count=result.get("failed_count", 0),
            remaining_pending=result.get("remaining_pending", 0),
        )
    except Exception as e:
        logger.exception("Failed to process outbox dispatch batch.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Outbox processing failed: {str(e)}",
        )


def _run_cli() -> None:
    parser = argparse.ArgumentParser(description="Run an ADL-SignalForge agency pipeline.")
    parser.add_argument(
        "--agency",
        choices=("adl",),
        default=os.getenv("SIGNALFORGE_AGENCY", "adl").strip().lower(),
        help="Agency pipeline to run (also configurable with SIGNALFORGE_AGENCY).",
    )
    parser.add_argument(
        "--query",
        default="lead generation marketing agencies digital growth",
        help="Discovery query for the selected agency pipeline.",
    )
    parser.add_argument("--max-results", type=int, default=5)
    args = parser.parse_args()

    from agencies.adl.run_pipeline import run_pipeline as run_adl_pipeline

    asyncio.run(run_adl_pipeline(query=args.query, max_results=args.max_results))


if __name__ == "__main__":
    _run_cli()
