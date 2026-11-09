"""
ADL-SignalForge Persistence Sink Module
Path: agencies/adl/persistence/sink.py
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("adl.signalforge.sink")


class JSONLSink:
    """
    Persists normalized agency candidate records to a JSON Lines (JSONL) file on disk.
    """

    def __init__(self, output_path: str = "data/output/normalized_agencies.jsonl") -> None:
        self.output_path = Path(output_path)
        # Ensure target output directory exists
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

    def write_records(self, records: List[Dict[str, Any]]) -> int:
        """
        Synchronously appends a batch of records to the JSONL file.
        Designed to be run via run_in_executor to keep the async loop unblocked.
        """
        if not records:
            return 0

        written_count = 0
        try:
            with open(self.output_path, "a", encoding="utf-8") as f:
                for record in records:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    written_count += 1
            logger.info("Successfully persisted records to disk", extra={"count": written_count, "path": str(self.output_path)})
        except Exception as e:
            logger.error("Failed to write records to JSONL sink", exc_info=e)

        return written_count
