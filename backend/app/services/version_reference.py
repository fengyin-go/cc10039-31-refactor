"""版本引用核对：收拢 Schema 审批与快照废弃对快照版本的关联查询。

历史上 Schema 演进的「提交审批」和快照版本的「废弃快照」各自查询了一遍
快照引用数据，两边的版本关联与可用性判定容易漂移，且"先查后写"在并发下
会留下已提交却无法读取的审批引用。本模块把两类查询合并为同一份
``VersionReferenceCheck`` 核对结果，两个领域服务都消费它：

- 审批提交：通过 ``available`` 与 ``snapshot_id`` 定位有效快照；
- 快照废弃：通过 ``pending_references`` 核实是否仍有待处理引用。

判定规则保持原有行为：仅状态为「可用」的快照视为可用；仅状态为
「草稿」「待审批」的 Schema 变更视为待处理引用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.store import store

# 仍占用快照版本、阻止废弃的 Schema 变更状态（沿用原 discard_snapshot 判定）
PENDING_SCHEMA_STATUSES: tuple[str, ...] = ("草稿", "待审批")


@dataclass
class VersionReferenceCheck:
    """版本引用核对结果：同一份数据同时服务审批提交与快照废弃。"""

    version_no: str
    table_name: str
    # 快照关联结果
    snapshot_exists: bool
    snapshot_id: int | None
    snapshot_status: str | None
    # 可用性核对：快照存在且状态为「可用」
    available: bool
    # 仍待处理（草稿/待审批）的 Schema 变更引用
    pending_references: list[dict[str, Any]] = field(default_factory=list)

    @property
    def pending_reference_count(self) -> int:
        return len(self.pending_references)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_no": self.version_no,
            "table_name": self.table_name,
            "snapshot_exists": self.snapshot_exists,
            "snapshot_id": self.snapshot_id,
            "snapshot_status": self.snapshot_status,
            "available": self.available,
            "pending_reference_count": self.pending_reference_count,
            "pending_references": list(self.pending_references),
        }


def check_version_reference(
    version_no: str, table_name: str = ""
) -> VersionReferenceCheck:
    """核对某个快照版本号的关联关系与可用性。

    :param version_no: 快照版本号（对应快照的「版本号」、Schema 变更的
        「变更前快照」字段）。
    :param table_name: 可选湖表名，传入时快照定位限定在该湖表内；
        Schema 引用仍按版本号全局核对，与历史行为一致。
    """
    version_no = str(version_no or "").strip()
    table_name = str(table_name or "").strip()

    snapshots = (
        store.find_by_field("snapshot_version", "版本号", version_no)
        if version_no
        else []
    )
    if table_name:
        snapshots = [s for s in snapshots if s.get("湖表名") == table_name]

    snapshot = snapshots[0] if snapshots else None
    snapshot_exists = snapshot is not None
    snapshot_id = int(snapshot["id"]) if snapshot_exists else None
    snapshot_status = snapshot.get("status") if snapshot_exists else None
    available = snapshot_exists and snapshot_status == "可用"

    pending_references: list[dict[str, Any]] = []
    if version_no:
        for change in store.find_by_field(
            "schema_evolution", "变更前快照", version_no
        ):
            if change.get("status") in PENDING_SCHEMA_STATUSES:
                pending_references.append({
                    "id": change.get("id"),
                    "湖表名": change.get("湖表名", ""),
                    "status": change.get("status"),
                })

    return VersionReferenceCheck(
        version_no=version_no,
        table_name=table_name,
        snapshot_exists=snapshot_exists,
        snapshot_id=snapshot_id,
        snapshot_status=snapshot_status,
        available=available,
        pending_references=pending_references,
    )
