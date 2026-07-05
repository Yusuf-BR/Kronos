import logging
import time
import shutil
import os
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from agents.extractor import ExtractorAgent
from agents.codex import CodexAgent
from agents.reconciler import ReconcilerAgent
from core.config import config
from utils.registry import is_duplicate, mark_processed, mark_failed
from utils.retry import call_with_retry

logger = logging.getLogger(__name__)


class PDFHandler(FileSystemEventHandler):
    def __init__(self):
        self.extractor = ExtractorAgent()
        self.codex = CodexAgent()
        self.reconciler = ReconcilerAgent()
        self.processing = set()
        logger.info("PDFHandler initialized")

    def on_created(self, event):
        if event.is_directory:
            return
        if not event.src_path.endswith(".pdf"):
            return
        time.sleep(1)
        self._process(event.src_path)

    def _process(self, filepath: str):
        filename = Path(filepath).name

        if filename in self.processing:
            logger.info(f"Already processing {filename}, skipping")
            return

        # Deduplication check
        if is_duplicate(filepath):
            logger.warning(f"Duplicate detected: {filename} already processed. Skipping.")
            shutil.move(filepath, os.path.join(config.PROCESSED_PATH, filename))
            return

        self.processing.add(filename)
        logger.info(f"New PDF detected: {filename}")

        try:
            # Stage 1 — Extract
            try:
                extracted = call_with_retry(
                    self.extractor.extract_knowledge,
                    filepath,
                    max_attempts=3,
                    base_delay=2.0
                )
            except Exception as e:
                mark_failed(filepath, e, stage="extraction")
                logger.error(f"Extraction failed permanently for {filename}: {e}")
                try:
                    quarantine = "data/quarantine"
                    os.makedirs(quarantine, exist_ok=True)
                    shutil.move(filepath, os.path.join(quarantine, filename))
                    logger.warning(f"  Moved {filename} to data/quarantine/")
                except Exception as move_err:
                    logger.warning(f"  Could not move {filename} to quarantine: {move_err}")
                return

            # Quality gate — reject empty extractions
            page_count = len(extracted.get("pages", [])) if isinstance(extracted.get("pages"), list) else extracted.get("metadata", {}).get("page_count", 0)
            chunk_count = len(extracted.get("chunks", []))

            if page_count == 0 and chunk_count == 0:
                error_msg = "Empty extraction — 0 pages extracted. Possibly corrupted or scanned PDF."
                mark_failed(filepath, error_msg, stage="quality_gate")
                logger.error(f"Quality gate failed for {filename}: {error_msg}")
                quarantine = "data/quarantine"
                os.makedirs(quarantine, exist_ok=True)
                shutil.move(filepath, os.path.join(quarantine, filename))
                logger.warning(f"  Moved {filename} to data/quarantine/")
                return

            logger.info(f"  Quality gate passed: {chunk_count} chunks extracted")

            # Stage 2 — Reconcile
            try:
                reconcile_result = call_with_retry(
                    self.reconciler.reconcile,
                    extracted,
                    max_attempts=3,
                    base_delay=2.0
                )
                if reconcile_result["conflicts_found"] > 0:
                    logger.warning(f"  {reconcile_result['conflicts_found']} conflicts found in {filename}")
            except Exception as e:
                logger.warning(f"Reconciliation failed for {filename}: {e} — continuing with ingestion")

            # Stage 3 — Ingest
            try:
                ingest_result = call_with_retry(
                    self.codex.ingest,
                    extracted,
                    max_attempts=3,
                    base_delay=2.0
                )
                logger.info(f"  Ingested: {ingest_result}")
            except Exception as e:
                mark_failed(filepath, e, stage="ingestion")
                logger.error(f"Ingestion failed permanently for {filename}: {e}")
                return

            # Success
            mark_processed(filepath, ingest_result)
            processed_path = os.path.join(config.PROCESSED_PATH, filename)
            shutil.move(filepath, processed_path)
            logger.info(f"  Completed: {filename} → processed/")

        except Exception as e:
            mark_failed(filepath, e, stage="unknown")
            logger.error(f"Unexpected failure for {filename}: {e}")
        finally:
            self.processing.discard(filename)

    def process_backlog(self, inbox_path: str):
        pdfs = list(Path(inbox_path).glob("*.pdf"))
        if pdfs:
            logger.info(f"Processing backlog: {len(pdfs)} PDFs found")
            for pdf in pdfs:
                self._process(str(pdf))
        else:
            logger.info("No backlog found")

    def close(self):
        self.codex.close()
        self.reconciler.close()


class WatcherAgent:
    def __init__(self):
        self.inbox = config.INBOX_PATH
        self.handler = PDFHandler()
        self.observer = Observer()
        logger.info("WatcherAgent initialized")

    def start(self):
        os.makedirs(self.inbox, exist_ok=True)
        os.makedirs(config.PROCESSED_PATH, exist_ok=True)
        os.makedirs("data/quarantine", exist_ok=True)

        self.handler.process_backlog(self.inbox)

        self.observer.schedule(self.handler, self.inbox, recursive=False)
        self.observer.start()
        logger.info(f"Watching: {self.inbox}")

        try:
            while True:
                time.sleep(2)
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        self.observer.stop()
        self.observer.join()
        self.handler.close()
        logger.info("Watcher stopped")