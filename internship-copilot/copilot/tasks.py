"""Background tasks: one LLM job at a time (a laptop runs one local model well), progress steps, cancellation."""
from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
import uuid
from typing import Callable, Optional

from .fetch import FetchError
from .llm import Cancelled, LLMError

log = logging.getLogger("copilot.tasks")


class Task:
    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.status = "queued"  # queued | running | done | error | cancelled
        self.steps: list[dict] = []
        self.detail = ""
        self.result: Optional[dict] = None
        self.error = ""
        self.error_kind = ""
        self.cancel = threading.Event()
        self.created = time.time()
        self.finished = 0.0
        self._lock = threading.Lock()

    # ---- used by pipeline code ----
    def step(self, key: str, label: str) -> None:
        with self._lock:
            for s in self.steps:
                if s["status"] == "running":
                    s["status"] = "done"
            self.steps.append({"key": key, "label": label, "status": "running"})
            self.detail = ""
        self.check()

    def done_step(self, detail: str = "") -> None:
        with self._lock:
            for s in reversed(self.steps):
                if s["status"] == "running":
                    s["status"] = "done"
                    if detail:
                        s["detail"] = detail
                    break

    def tokens(self, n: int) -> None:
        self.detail = f"{n} tokens generated"

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    def to_dict(self, position: int = 0) -> dict:
        with self._lock:
            return {"id": self.id, "kind": self.kind, "status": self.status, "steps": [dict(s) for s in self.steps],
                    "detail": self.detail, "result": self.result, "error": self.error, "error_kind": self.error_kind,
                    "queue_position": position, "elapsed": round((self.finished or time.time()) - self.created, 1)}


class TaskManager:
    def __init__(self, keep: int = 60):
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []
        self._q: "queue.Queue[tuple[Task, Callable[[Task], dict]]]" = queue.Queue()
        self._lock = threading.Lock()
        self._keep = keep
        self._worker: Optional[threading.Thread] = None

    def _ensure_worker(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._loop, name="copilot-worker", daemon=True)
        self._worker.start()

    def submit(self, kind: str, fn: Callable[[Task], dict]) -> Task:
        t = Task(kind)
        with self._lock:
            self._tasks[t.id] = t
            self._order.append(t.id)
            while len(self._order) > self._keep:
                self._tasks.pop(self._order.pop(0), None)
        self._q.put((t, fn))
        self._ensure_worker()
        return t

    def get(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def position(self, task: Task) -> int:
        if task.status != "queued":
            return 0
        with self._q.mutex:
            pending = [t.id for t, _ in list(self._q.queue)]
        return pending.index(task.id) + 1 if task.id in pending else 0

    def cancel(self, task_id: str) -> bool:
        t = self._tasks.get(task_id)
        if not t or t.status in ("done", "error", "cancelled"):
            return False
        t.cancel.set()
        return True

    def busy(self) -> bool:
        return any(t.status in ("queued", "running") for t in self._tasks.values())

    def _loop(self) -> None:
        while True:
            task, fn = self._q.get()
            if task.cancel.is_set():
                task.status, task.finished = "cancelled", time.time()
                continue
            task.status = "running"
            try:
                task.result = fn(task)
                task.done_step()
                task.status = "done"
            except Cancelled:
                task.status = "cancelled"
            except (LLMError, FetchError) as exc:
                task.status, task.error = "error", str(exc)
                task.error_kind = getattr(exc, "kind", "") or ("blocked" if getattr(exc, "blocked", False) else "")
            except Exception as exc:  # unexpected: keep the app alive, show a readable message
                log.error("Task %s failed:\n%s", task.id, traceback.format_exc())
                task.status, task.error = "error", f"Unexpected error: {type(exc).__name__}: {exc}"
            finally:
                task.finished = time.time()
