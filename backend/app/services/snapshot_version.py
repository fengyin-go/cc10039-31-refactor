"""快照版本领域服务：管理湖表数据的快照版本和基于时间点的数据恢复。

上游接收 schema 演进和回填任务的事件，下游为治理报告提供版本追溯数据。
每个快照带版本号和时间戳，支持废弃管理和版本对比。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.version_reference import check_version_reference
from app.store import store


class SnapshotVersionService(BaseModuleService):
    MODULE = "snapshot_version"
    ENTITY = "快照版本"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["湖表名", "快照时间戳", "版本号", "记录数", "快照类型"]
    REQUIRED_FIELDS = ["湖表名", "快照时间戳", "版本号"]
    STATUS_ORDER = ["生成中", "可用", "已废弃"]
    ACTION_RULES = {"生成快照": "可用", "废弃快照": "已废弃"}
    NEGATIVE_ACTIONS = []

    def generate_snapshot(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """生成快照版本：创建模块数据深拷贝并记录版本。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "快照版本不存在"
        if entry.get("status") != "生成中":
            return None, f"快照版本状态为{entry.get('status')}，不可生成"

        table_name = entry.get("湖表名", "")
        version_no = entry.get("版本号", "")

        # 检查同湖表是否有同名版本快照
        existing = [
            sv for sv in store.rows(self.MODULE)
            if sv.get("湖表名") == table_name
            and sv.get("版本号") == version_no
            and sv.get("id") != entry_id
        ]
        if existing:
            return None, f"湖表 {table_name} 已存在版本 {version_no} 的快照，不可重复生成"

        # 创建底层数据快照
        snapshot_key = store.create_snapshot(f"{table_name}_v{version_no}")

        entry["status"] = "可用"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["快照类型"] = "全量快照"

        store.log_event("snapshot_created", "snapshot_version", "snapshot_version", {
            "snapshot_id": entry_id,
            "table_name": table_name,
            "version_no": version_no,
            "snapshot_key": snapshot_key,
        })

        return entry, f"湖表 {table_name} 版本 {version_no} 快照已生成，key={snapshot_key}"

    def discard_snapshot(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """废弃快照版本：通过版本引用核对检查待处理依赖。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "快照版本不存在"
        if entry.get("status") != "可用":
            return None, f"快照版本状态为{entry.get('status')}，不可废弃"

        table_name = entry.get("湖表名", "")
        version_no = entry.get("版本号", "")

        # 锁外先消费一次版本引用核对结果，快速失败
        check = check_version_reference(version_no, table_name)
        if check.pending_references:
            return None, (
                f"版本 {version_no} 仍被 {check.pending_reference_count} 个"
                f"待处理 Schema 变更引用，不可废弃"
            )

        # 锁内复查同一份核对结果后再写入，防止并发提交审批造成
        # 快照已废弃而审批仍引用该版本的不一致结果
        with store.transaction():
            current = store.find(self.MODULE, entry_id)
            if current is None or current.get("status") != "可用":
                return None, "快照版本状态已变化，不可废弃"

            recheck = check_version_reference(
                current.get("版本号", ""), current.get("湖表名", "")
            )
            if recheck.pending_references:
                return None, (
                    f"版本 {recheck.version_no} 仍被 {recheck.pending_reference_count} 个"
                    f"待处理 Schema 变更引用，不可废弃"
                )

            current["status"] = "已废弃"
            current["pending"] = False
            current["abnormal"] = False

            store.log_event("snapshot_discarded", "snapshot_version", "schema_evolution", {
                "snapshot_id": entry_id,
                "table_name": current.get("湖表名", ""),
                "version_no": current.get("版本号", ""),
            })

            return current, f"快照版本 {current.get('版本号', '')} 已废弃"

    def available_snapshots_for_table(self, table_name: str) -> list[dict[str, Any]]:
        """查询指定湖表的可用快照列表。"""
        return [
            sv for sv in store.rows(self.MODULE)
            if sv.get("湖表名") == table_name and sv.get("status") == "可用"
        ]

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "生成快照":
            return self.generate_snapshot(entry_id)
        if action == "废弃快照":
            return self.discard_snapshot(entry_id)
        return super().run_action(entry_id, action)