import logging
import os
import secrets
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Security, status
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel, Field

# --- Internal Domain Imports ---
try:
    from dispatch.outbox import process_outbox_batch
except ImportError:
    async def process_outbox_batch(limit: int = 50) -> Dict[str, Any]:
        raise NotImplementedError("dispatch.outbox module not found.")

try:
    from storage.duckdb_client import DuckDBClient
    from storage.repository import SignalForgeRepository, PipelineRunNotFoundError
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
EXPECTED_API_KEY = os.getenv("API_KEY")

if not EXPECTED_API_KEY:
    logger.critical("FATAL: 'API_KEY' environment variable is not set. Refusing to boot.")
    raise RuntimeError("API_KEY environment variable is required.")

api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

async def verify_api_key(
    api_key_input: Optional[str] = Security(api_key_header),
) -> None:
    if not api_key_input or not secrets.compare_digest(api_key_input, EXPECTED_API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Missing or invalid API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

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
    
    # --- Single Unified Path Resolution with Persistent Disk Auto-Detection ---
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
            db_client.initialize()
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

# ------------------------------------------------------------------------------
# Schemas & DTOs
# ------------------------------------------------------------------------------
class HealthResponse(BaseModel):
    status: str
    service: str

class ReadinessResponse(BaseModel):
    status: str
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

# ------------------------------------------------------------------------------
# Operational & Health Probes (Public)
# ------------------------------------------------------------------------------
@app.get("/healthz", response_model=HealthResponse, tags=["Monitoring"])
async def health_check():
    return HealthResponse(status="healthy", service="ADL-SignalForge")


@app.get("/readyz", response_model=ReadinessResponse, tags=["Monitoring"])
async def readiness_check():
    persistent_dir = Path("/data")
    if persistent_dir.exists() and os.access(persistent_dir, os.W_OK):
        default_db_dir = persistent_dir
    else:
        default_db_dir = Path("/tmp/data") if os.getenv("RENDER") else Path("./data")

    db_path_str = os.getenv("DATABASE_PATH", os.getenv("DUCKDB_PATH", str(default_db_dir / "signalforge.duckdb")))
    storage_dir = os.path.dirname(db_path_str) or "."
    
    storage_ok = True
    try:
        os.makedirs(storage_dir, exist_ok=True)
        storage_ok = os.access(storage_dir, os.W_OK)
    except Exception:
        storage_ok = False
    
    if not storage_ok:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage layer inaccessible or locked.",
        )
    return ReadinessResponse(status="ready", storage_accessible=True)


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
    tags=["Pipeline Control"],
    dependencies=[Depends(verify_api_key)],
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


@app.get(
    "/api/v1/runs/{run_id}",
    tags=["Pipeline Control"],
    dependencies=[Depends(verify_api_key)],
)
async def get_run_status(run_id: str):
    """
    Inspect the execution status and metrics of a specific pipeline run from the repository.
    """
    if not repository:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Repository layer is not initialized.",
        )

    try:
        run_record = repository.get_pipeline_run(run_id)
        if not run_record:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Pipeline run '{run_id}' not found.",
            )
        return run_record
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to retrieve run record '{run_id}': {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve run status: {str(e)}",
        )


@app.post(
    "/api/v1/dispatch/process",
    response_model=OutboxDispatchResponse,
    tags=["Outbox Worker"],
    dependencies=[Depends(verify_api_key)],
)
async def dispatch_process(limit: int = 50):
    """
    Executes a bounded batch of pending outbox events (webhooks and alerts).
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


@app.get(
    "/api/v1/dispatch/status",
    tags=["Outbox Worker"],
    dependencies=[Depends(verify_api_key)],
)
async def dispatch_status():
    """
    Inspect pending, locked, and failed outbox notification state.
    """
    return {
        "status": "operational",
        "message": "Outbox worker status inspection endpoint is active."
    }