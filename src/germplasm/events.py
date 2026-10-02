"""事件与事件日志。

系统只有一本只追加的 JSONL 事件日志；材料谱系、取用单、试验、告警、
待复核任务与处置责任全部是事件重放后的投影。进程重启后重新加载日志
即可恢复，告警与待办不会丢失。

同一条扫码或交接回执可能因网络重试、离线补传而提交多次，
以幂等键去重；离线补传保留事件实际发生时间（occurred_at），
登记时间（recorded_at）用于区分补传先后。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def event_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class Event:
    """一条不可变事件。

    occurred_at: 业务事实实际发生时间（离线补传时可能早于 recorded_at）
    recorded_at: 事件进入日志的时间
    idem_key:   提交方生成的幂等键，重复提交只生效一次
    """

    seq: int
    event_id: str
    type: str
    actor: str
    payload: dict[str, Any]
    occurred_at: str
    recorded_at: str = field(default_factory=lambda: utcnow().isoformat())
    idem_key: str | None = None

    def to_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_line(cls, line: str) -> "Event":
        data = json.loads(line)
        return cls(**data)


class EventStore:
    """只追加 JSONL 日志，带内存索引与重放回调。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._events: list[Event] = []
        self._idem: dict[str, int] = {}
        self._listeners: list[Callable[[Event], None]] = []
        self._lock = threading.RLock()
        if self._path is not None and self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._ingest(Event.from_line(line), persist=False)

    @property
    def path(self) -> Path | None:
        return self._path

    def listen(self, listener: Callable[[Event], None]) -> None:
        """注册投影回调；注册时先补发历史事件，保证状态一致。"""
        with self._lock:
            for event in self._events:
                listener(event)
            self._listeners.append(listener)

    def all(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def append(
        self,
        type: str,
        actor: str,
        payload: dict[str, Any],
        *,
        occurred_at: datetime | str | None = None,
        idem_key: str | None = None,
    ) -> Event:
        """登记一条事件。

        幂等键已存在时，原样返回首次登记的事件，不重复生效。
        """
        with self._lock:
            if idem_key is not None and idem_key in self._idem:
                return self._events[self._idem[idem_key]]
            occurred = (
                occurred_at.isoformat() if isinstance(occurred_at, datetime)
                else occurred_at or utcnow().isoformat()
            )
            event = Event(
                seq=len(self._events),
                event_id=event_id(),
                type=type,
                actor=actor,
                payload=payload,
                occurred_at=occurred,
                idem_key=idem_key,
            )
            self._ingest(event, persist=True)
            return event

    def _ingest(self, event: Event, *, persist: bool) -> None:
        if event.seq != len(self._events):
            raise ValueError("事件序号不连续，日志可能已损坏")
        self._events.append(event)
        if event.idem_key is not None:
            self._idem[event.idem_key] = event.seq
        if persist:
            if self._path is None:
                raise ValueError("内存日志无法持久化")
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(event.to_line() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        for listener in list(self._listeners):
            listener(event)

    @staticmethod
    def deterministic_key(*parts: Any) -> str:
        """由业务要素生成稳定幂等键，供扫码/回执天然去重。"""
        joined = "|".join(str(part) for part in parts)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]
