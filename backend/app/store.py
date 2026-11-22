"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
支持跨模块查询、快照记录和流水日志，用于支撑跨模块数据流。
"""
from __future__ import annotations

import copy
import datetime
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 跨模块事件日志：记录所有跨模块操作
        self._events: list[dict[str, Any]] = []
        # 模块快照备份：用于检查点和恢复
        self._snapshots: dict[str, list[dict[str, Any]]] = {}
        # 跨模块写操作的互斥锁：保证"核对 + 写入"原子完成，避免并发
        # 操作各自先查一次都获准、随后留下彼此不一致的关联记录。
        # 可重入，允许同一线程在临界区内嵌套调用。
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """跨模块写操作临界区：核对结果与状态流转在同一把锁内完成。"""
        self._lock.acquire()
        try:
            yield
        finally:
            self._lock.release()

    # ---- 基础读写 ----

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_field(self, module: str, field: str, value: str) -> list[dict[str, Any]]:
        """按指定字段值查找记录，支持跨模块关联查询。"""
        return [
            row for row in self.rows(module)
            if str(row.get(field, "")).casefold() == value.casefold()
        ]

    def upsert(self, module: str, entry: dict[str, Any]) -> dict[str, Any]:
        """插入或更新记录；存在相同 id 时覆盖，否则追加。"""
        rows = self.rows(module)
        entry_id = entry.get("id")
        for i, row in enumerate(rows):
            if row.get("id") == entry_id:
                rows[i] = entry
                return entry
        rows.append(entry)
        return entry

    # ---- 跨模块事件 ----

    def log_event(
        self, event_type: str, source_module: str, target_module: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """记录跨模块操作事件，供血缘和审计追溯。"""
        event = {
            "id": len(self._events) + 1,
            "event_type": event_type,
            "source_module": source_module,
            "target_module": target_module,
            "timestamp": datetime.datetime.now().isoformat(),
            "payload": payload,
        }
        self._events.append(event)
        return event

    def events_for_module(self, module: str) -> list[dict[str, Any]]:
        """查询与指定模块相关的所有事件。"""
        return [
            e for e in self._events
            if e["source_module"] == module or e["target_module"] == module
        ]

    def events_between(self, source: str, target: str) -> list[dict[str, Any]]:
        """查询两个模块之间的单向事件。"""
        return [
            e for e in self._events
            if e["source_module"] == source and e["target_module"] == target
        ]

    # ---- 快照与检查点 ----

    def create_snapshot(self, module: str) -> str:
        """为指定模块创建深拷贝快照，返回快照标识。"""
        timestamp = datetime.datetime.now().isoformat()
        key = f"{module}@{timestamp}"
        self._snapshots[key] = copy.deepcopy(self.rows(module))
        return key

    def get_snapshot(self, key: str) -> list[dict[str, Any]] | None:
        """按标识获取快照副本。"""
        raw = self._snapshots.get(key)
        return copy.deepcopy(raw) if raw is not None else None

    def snapshot_keys(self) -> list[str]:
        return sorted(self._snapshots)

    # ---- 概览 ----

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    def cross_module_stats(self) -> dict[str, object]:
        """跨模块统计：事件数、快照数、关联模块对。"""
        pairs: set[tuple[str, str]] = set()
        for e in self._events:
            pairs.add((e["source_module"], e["target_module"]))
        return {
            "event_count": len(self._events),
            "snapshot_count": len(self._snapshots),
            "linked_pairs": len(pairs),
            "linked_modules": sorted(set(
                m for p in pairs for m in p
            )),
        }


store = Store()
