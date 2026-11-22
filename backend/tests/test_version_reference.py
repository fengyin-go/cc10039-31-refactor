"""版本引用核对测试：统一核对结果 + 审批/废弃并发互斥。

两部分：
1. 基于旧样例核对三种结果：缺少快照、可用版本、等待审批的引用；
2. 并发执行同一版本的「提交审批」与「废弃快照」，验证只能留下
   彼此一致的结果——审批成功时快照必然仍可用，废弃成功时审批必然
   失败且记录保留，历史仍能按原编号找到当时引用。
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.store import store
from app.services.snapshot_version import SnapshotVersionService
from app.services.schema_evolution import SchemaEvolutionService
from app.services.version_reference import check_version_reference


class TestVersionReferenceCheck:
    """基于旧样例的版本引用核对结果。"""

    def setup_method(self):
        self.snap_svc = SnapshotVersionService()
        self.schema_svc = SchemaEvolutionService()
        self._created_ids: list[tuple[str, int]] = []

    def teardown_method(self):
        for module, entry_id in self._created_ids:
            rows = store.rows(module)
            rows[:] = [row for row in rows if row.get("id") != entry_id]

    def _create_schema(self, version_ref: str, table_name: str) -> int:
        entry, missing = self.schema_svc.create_entry({
            "湖表名": table_name,
            "变更类型": "添加字段",
            "变更字段": "新字段",
            "兼容级别": "向后兼容",
            "变更前快照": version_ref,
        })
        assert entry is not None and not missing
        self._created_ids.append(("schema_evolution", entry["id"]))
        return entry["id"]

    def test_missing_snapshot_result(self):
        """缺少快照：核对结果标记不存在、不可用，且无待处理引用。"""
        missing_version = "旧样例中不存在的版本号-vref"
        check = check_version_reference(missing_version)

        assert check.version_no == missing_version
        assert check.snapshot_exists is False
        assert check.snapshot_id is None
        assert check.snapshot_status is None
        assert check.available is False
        assert check.pending_references == []
        assert check.pending_reference_count == 0

        # 审批提交消费同一份核对结果：引用缺失时按原消息拒绝
        entry_id = self._create_schema(missing_version, "核对缺快照湖表")
        result, msg = self.schema_svc.submit_approval(entry_id)
        assert result is None
        assert msg == f"变更前快照 {missing_version} 不可用"
        # 被拒绝的审批仍是草稿，记录保留
        assert store.find("schema_evolution", entry_id)["status"] == "草稿"

    def test_available_version_result(self):
        """可用版本：旧样例快照 2（状态=可用）可被核对定位。"""
        check = check_version_reference("快照版本样例2")

        assert check.snapshot_exists is True
        assert check.snapshot_id == 2
        assert check.snapshot_status == "可用"
        assert check.available is True

        # 审批可定位到有效快照并提交成功
        entry_id = self._create_schema("快照版本样例2", "快照版本样例2")
        result, msg = self.schema_svc.submit_approval(entry_id)
        try:
            assert result is not None, msg
            assert result["status"] == "待审批"
            # 审批成功后核对结果仍指向同一张可用快照
            recheck = check_version_reference("快照版本样例2")
            assert recheck.available is True
            assert recheck.snapshot_id == 2
        finally:
            # 还原为草稿，避免待审批引用影响其它用例废弃该样例快照
            store.find("schema_evolution", entry_id)["status"] = "草稿"

    def test_pending_approval_reference_result(self):
        """等待审批：旧样例 Schema 变更 2（待审批）出现在待处理引用中。"""
        check = check_version_reference("Schema演进样例2")

        # 该版本本身没有快照，但有待审批的 Schema 变更引用
        assert check.snapshot_exists is False
        assert check.available is False
        pending_ids = [ref["id"] for ref in check.pending_references]
        assert 2 in pending_ids
        pending_seed = next(ref for ref in check.pending_references if ref["id"] == 2)
        assert pending_seed["status"] == "待审批"

    def test_discard_blocked_by_pending_reference(self):
        """废弃前能核实待处理引用：消息与原行为一致，快照保持可用。"""
        # 给可用样例快照 2 挂一个草稿态引用
        entry_id = self._create_schema("快照版本样例2", "快照版本样例2")

        check = check_version_reference("快照版本样例2", "快照版本样例2")
        assert check.available is True
        assert check.pending_reference_count >= 1
        assert entry_id in [ref["id"] for ref in check.pending_references]

        result, msg = self.snap_svc.discard_snapshot(2)
        assert result is None
        assert "版本 快照版本样例2 仍被" in msg
        assert "个待处理 Schema 变更引用，不可废弃" in msg
        assert store.find("snapshot_version", 2)["status"] == "可用"


class TestVersionReferenceConcurrency:
    """同一版本上审批提交与快照废弃的并发互斥。"""

    ROUNDS = 40

    def setup_method(self):
        self.snap_svc = SnapshotVersionService()
        self.schema_svc = SchemaEvolutionService()
        self._created: list[tuple[str, int]] = []

    def teardown_method(self):
        for module, entry_id in self._created:
            rows = store.rows(module)
            rows[:] = [row for row in rows if row.get("id") != entry_id]

    def _next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in store.rows(module)), default=0) + 1

    def _make_snapshot(self, round_no: int) -> tuple[int, str, str]:
        version = f"并发版本-vref-{round_no}"
        table = f"并发湖表-vref-{round_no}"
        snap_id = self._next_id("snapshot_version")
        store.rows("snapshot_version").append({
            "id": snap_id,
            "status": "可用",
            "pending": False,
            "abnormal": False,
            "湖表名": table,
            "快照时间戳": "2026-11-22",
            "版本号": version,
            "记录数": "100",
            "快照类型": "全量快照",
        })
        self._created.append(("snapshot_version", snap_id))
        return snap_id, version, table

    def _make_schema_row(self, schema_id: int, version: str, table: str) -> None:
        store.rows("schema_evolution").append({
            "id": schema_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "湖表名": table,
            "变更类型": "添加字段",
            "变更字段": "并发字段",
            "兼容级别": "向后兼容",
            "变更前快照": version,
        })
        self._created.append(("schema_evolution", schema_id))

    def _assert_consistent_final_state(
        self, round_no: int, snap_id: int, schema_id: int, version: str,
        submit_ok: bool, submit_msg: str, discard_ok: bool, discard_msg: str,
    ) -> None:
        snapshot = store.find("snapshot_version", snap_id)
        schema_change = store.find("schema_evolution", schema_id)

        # 记录都还在，且历史仍按原编号保留当时引用
        assert snapshot is not None and schema_change is not None
        assert snapshot["版本号"] == version
        assert schema_change["变更前快照"] == version
        assert snapshot["status"] in ("可用", "已废弃")
        assert schema_change["status"] in ("草稿", "待审批")

        # 互斥：两个操作不可能同时成功，也不可能同时失败
        assert submit_ok is not discard_ok, (
            f"第{round_no}轮结果非互斥：submit={submit_ok}, discard={discard_ok}, "
            f"消息={submit_msg} / {discard_msg}"
        )

        if submit_ok:
            # 审批成功时，快照必然仍可用，不存在无法读取的审批引用
            assert schema_change["status"] == "待审批"
            assert snapshot["status"] == "可用"
            assert check_version_reference(version, snapshot["湖表名"]).available is True
        else:
            # 审批失败：消息沿用原口径，变更留在草稿
            assert submit_msg == f"变更前快照 {version} 不可用"
            assert schema_change["status"] == "草稿"

        if discard_ok:
            # 废弃成功时，审批必然未提交：引用方只能停留在草稿，
            # 不存在任何"待审批"状态指向已废弃快照的悬空引用
            assert snapshot["status"] == "已废弃"
            assert schema_change["status"] == "草稿"
            refs = check_version_reference(
                version, snapshot["湖表名"]
            ).pending_references
            assert all(ref["status"] == "草稿" for ref in refs)
            assert not any(ref["status"] == "待审批" for ref in refs)
        else:
            # 废弃失败：消息沿用原口径，快照保持可用
            assert f"版本 {version} 仍被" in discard_msg
            assert "不可废弃" in discard_msg
            assert snapshot["status"] == "可用"

    def test_existing_draft_always_blocks_discard(self):
        """草稿引用已存在时：审批可成功，废弃始终被挡且无悬空引用。"""
        for round_no in range(1, 6):
            snap_id, version, table = self._make_snapshot(round_no)
            schema_id = self._next_id("schema_evolution")
            self._make_schema_row(schema_id, version, table)
            barrier = threading.Barrier(2)
            outcomes: dict[str, tuple[bool, str]] = {}

            def submit() -> None:
                barrier.wait(timeout=10)
                entry, msg = self.schema_svc.submit_approval(schema_id)
                outcomes["submit"] = (entry is not None, msg)

            def discard() -> None:
                barrier.wait(timeout=10)
                entry, msg = self.snap_svc.discard_snapshot(snap_id)
                outcomes["discard"] = (entry is not None, msg)

            t_submit = threading.Thread(target=submit)
            t_discard = threading.Thread(target=discard)
            t_submit.start()
            t_discard.start()
            t_submit.join(timeout=10)
            t_discard.join(timeout=10)

            submit_ok, submit_msg = outcomes["submit"]
            discard_ok, discard_msg = outcomes["discard"]
            assert submit_ok is True and discard_ok is False
            self._assert_consistent_final_state(
                round_no, snap_id, schema_id, version,
                submit_ok, submit_msg, discard_ok, discard_msg,
            )

    def test_racing_create_submit_and_discard_leave_consistent_results(self):
        """建档/提交与废弃真正竞速：复现"各自先查一次都获准"的窗口。

        废弃线程的引用核对可能跑在引用方建档之前而获准；没有临界区保护时
        双方都会继续写入，留下已提交却无法读取的审批引用。加锁后只能留下
        彼此一致的一种结果，且多轮竞速下两种胜出路径都应出现。
        """
        import random

        observed_submit_ok = False
        observed_discard_ok = False
        base = 1000

        for round_no in range(1, self.ROUNDS + 1):
            snap_id, version, table = self._make_snapshot(base + round_no)
            schema_id = self._next_id("schema_evolution")
            self._created.append(("schema_evolution", schema_id))
            barrier = threading.Barrier(2)
            outcomes: dict[str, tuple[bool, str]] = {}

            def submit_flow() -> None:
                barrier.wait(timeout=10)
                # 模拟"还在提交的变更审批"：建档与提交连续发生，
                # 可能晚于废弃线程的引用核对
                self._make_schema_row(schema_id, version, table)
                entry, msg = self.schema_svc.submit_approval(schema_id)
                outcomes["submit"] = (entry is not None, msg)

            def discard_flow() -> None:
                barrier.wait(timeout=10)
                entry, msg = self.snap_svc.discard_snapshot(snap_id)
                outcomes["discard"] = (entry is not None, msg)

            threads = [
                threading.Thread(target=submit_flow),
                threading.Thread(target=discard_flow),
            ]
            random.shuffle(threads)
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
                assert not t.is_alive()

            submit_ok, submit_msg = outcomes["submit"]
            discard_ok, discard_msg = outcomes["discard"]
            observed_submit_ok = observed_submit_ok or submit_ok
            observed_discard_ok = observed_discard_ok or discard_ok

            self._assert_consistent_final_state(
                base + round_no, snap_id, schema_id, version,
                submit_ok, submit_msg, discard_ok, discard_msg,
            )

        assert observed_submit_ok, "并发竞速中未观察到审批成功路径"
        assert observed_discard_ok, "并发竞速中未观察到废弃成功路径"

    # ---- 确定性交错：强制"复查后、写入前"的危险窗口 ----

    def _build_interleaving_hooks(
        self, monkeypatch, discard_thread: threading.Thread,
        recheck_reached: threading.Event,
    ) -> tuple[threading.Event, threading.Event]:
        """给两个服务消费的核对函数加阻塞钩子。

        废弃线程在临界区内做完引用复查后置位 recheck_reached，并停在
        write_gate 前；提交线程进入自己的临界区（完成一次核对）时置位
        submit_entered。
        """
        import app.services.snapshot_version as snap_mod
        import app.services.schema_evolution as schema_mod

        real_check = check_version_reference
        write_gate = threading.Event()
        submit_entered = threading.Event()

        def wrapper(version_no: str, table_name: str = ""):
            result = real_check(version_no, table_name)
            if threading.current_thread() is discard_thread:
                # 废弃线程的第二次核对即锁内复查，之后放行前先挂起
                wrapper.discard_calls += 1
                if wrapper.discard_calls == 2:
                    recheck_reached.set()
                    write_gate.wait(timeout=10)
            else:
                # 提交线程的第二次核对才发生在临界区内
                wrapper.submit_calls += 1
                if wrapper.submit_calls == 2:
                    submit_entered.set()
            return result
        wrapper.discard_calls = 0
        wrapper.submit_calls = 0

        monkeypatch.setattr(snap_mod, "check_version_reference", wrapper)
        monkeypatch.setattr(schema_mod, "check_version_reference", wrapper)
        return write_gate, submit_entered

    def test_deterministic_interleaving_is_serialized_by_lock(self, monkeypatch):
        """加锁时：废弃复查后挂起，提交只能排队，结果必然彼此一致。"""
        snap_id, version, table = self._make_snapshot(2000)
        schema_id = self._next_id("schema_evolution")
        self._created.append(("schema_evolution", schema_id))

        recheck_reached = threading.Event()
        outcomes: dict[str, tuple[bool, str]] = {}

        def discard_flow() -> None:
            entry, msg = self.snap_svc.discard_snapshot(snap_id)
            outcomes["discard"] = (entry is not None, msg)

        t_discard = threading.Thread(target=discard_flow)
        write_gate, submit_entered = self._build_interleaving_hooks(
            monkeypatch, t_discard, recheck_reached
        )
        t_discard.start()
        # 等废弃线程进入临界区并完成复查、停在写入前
        assert recheck_reached.wait(timeout=10)
        # 此时引用方尚未建档，废弃获准；再启动提交线程，它应被锁挡在外面
        self._make_schema_row(schema_id, version, table)

        def submit_flow() -> None:
            entry, msg = self.schema_svc.submit_approval(schema_id)
            outcomes["submit"] = (entry is not None, msg)

        t_submit = threading.Thread(target=submit_flow)
        t_submit.start()
        # 提交线程拿不到锁，不应进入临界区
        assert not submit_entered.wait(timeout=0.5)
        # 放行废弃写入，随后两个线程依次结束
        write_gate.set()
        t_discard.join(timeout=10)
        t_submit.join(timeout=10)
        assert not t_discard.is_alive() and not t_submit.is_alive()

        submit_ok, submit_msg = outcomes["submit"]
        discard_ok, discard_msg = outcomes["discard"]
        # 废弃先完成 → 提交复查到不可用快照而失败，无悬空审批引用
        assert discard_ok is True and submit_ok is False
        self._assert_consistent_final_state(
            2000, snap_id, schema_id, version,
            submit_ok, submit_msg, discard_ok, discard_msg,
        )

    def test_deterministic_interleaving_without_lock_reproduces_dangling_ref(
        self, monkeypatch,
    ):
        """模拟去掉临界区：同样的交错必然留下"已提交但无法读取"的引用。

        该用例固定无锁执行，证明锁是互斥结果的必要条件；一旦临界区被
        移除，本用例失败即说明悬空引用缺陷回归。
        """
        snap_id, version, table = self._make_snapshot(3000)
        schema_id = self._next_id("schema_evolution")
        self._created.append(("schema_evolution", schema_id))

        # 用空上下文替换事务临界区，等价于改造前的无锁"先查后写"
        from contextlib import nullcontext
        monkeypatch.setattr(store, "transaction", lambda: nullcontext())

        recheck_reached = threading.Event()
        outcomes: dict[str, tuple[bool, str]] = {}

        def discard_flow() -> None:
            entry, msg = self.snap_svc.discard_snapshot(snap_id)
            outcomes["discard"] = (entry is not None, msg)

        t_discard = threading.Thread(target=discard_flow)
        write_gate, submit_entered = self._build_interleaving_hooks(
            monkeypatch, t_discard, recheck_reached
        )
        t_discard.start()
        assert recheck_reached.wait(timeout=10)

        # 废弃已复查通过但尚未写入：提交线程此刻完整跑完并成功
        self._make_schema_row(schema_id, version, table)

        def submit_flow() -> None:
            entry, msg = self.schema_svc.submit_approval(schema_id)
            outcomes["submit"] = (entry is not None, msg)

        t_submit = threading.Thread(target=submit_flow)
        t_submit.start()
        t_submit.join(timeout=10)
        assert not t_submit.is_alive()
        assert submit_entered.wait(timeout=10)

        submit_ok, submit_msg = outcomes["submit"]
        # 无锁交错下提交应成功
        assert submit_ok is True, submit_msg

        # 放行废弃写入：双方都成功，产生悬空的待审批引用
        write_gate.set()
        t_discard.join(timeout=10)
        discard_ok, _ = outcomes["discard"]
        assert discard_ok is True

        snapshot = store.find("snapshot_version", snap_id)
        schema_change = store.find("schema_evolution", schema_id)
        assert snapshot["status"] == "已废弃"
        assert schema_change["status"] == "待审批"
        # 悬空引用：审批已提交，但其快照版本已不可用
        assert check_version_reference(version, table).available is False
