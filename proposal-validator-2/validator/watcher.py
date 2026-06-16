"""
File-system watcher using watchdog.
Fires a threading.Event the moment results.json is created or written.
"""
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


def watch_for_results(results_path: str | Path, ready_event: threading.Event):
    """
    Start a watchdog Observer that sets ready_event when results_path appears.
    Returns (observer, handler) — call observer.stop() / observer.join() when done.
    """
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

    target = Path(results_path).resolve()
    watch_dir = target.parent

    class _Handler(FileSystemEventHandler):
        def _hit(self, path: str) -> None:
            if Path(path).resolve() == target:
                logger.info("Detected results.json at %s", path)
                ready_event.set()

        def on_created(self, event):
            if not event.is_directory:
                self._hit(event.src_path)

        def on_modified(self, event):
            if not event.is_directory:
                self._hit(event.src_path)

    handler = _Handler()
    observer = Observer()
    observer.schedule(handler, str(watch_dir), recursive=False)
    observer.start()
    logger.info("Watching %s for results.json", watch_dir)
    return observer, handler
