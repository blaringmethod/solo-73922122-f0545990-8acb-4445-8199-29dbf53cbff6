"""核查轮次(会话)管理:初查建轮、续查接力、并发互斥。

- 初查证据有效后创建轮次,保存终点引擎状态、两份目标 CSV 与累计吻合区间;
  轮次只活在同一服务进程内存中,进程重启即失效。
- 续查在轮次锁内校验并应用:起点必须等于当前终点;在引擎克隆上重放,
  整包通过后才原子替换轮次状态(校验期间查看到的仍是上次结果);
  缺号/前值不符整批退回,原终点、两表状态与结果全部保留,改正后可从原终点重传。
- 两个页面同时从同一终点提交有效续包时,先持锁者生效,后到者发现
  终点已变,收到 stale_endpoint。
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass

from .core import (
    Engine,
    EvidenceError,
    InputError,
    intersect_intervals,
    merge_points_into_intervals,
    parse_continuation,
    replay_continuation,
)

MAX_ROUNDS = 128  # 进程内最多保留的轮次数,超出按最久未用淘汰


@dataclass
class RoundState:
    """轮次的可变状态,整体替换(不原地改),保证读取方看到一致快照。"""

    engine: Engine
    end_commit: int
    intervals_a: list   # 运单 CSV 累计吻合区间
    intervals_b: list   # 签收 CSV 累计吻合区间
    changes_applied: int
    batches: int        # 已应用的续查批数


class Round:
    def __init__(self, engine: Engine, start_commit: int, snapshot_rows: dict,
                 result: dict):
        self.id = uuid.uuid4().hex[:16]
        self.lock = threading.Lock()
        self.orig_start = start_commit          # 初查起点,整轮不变
        self.snapshot_rows = snapshot_rows      # 初查快照行数,整轮不变
        self.csv_rows = {"waybill_csv": len(engine.tgt_a),
                         "receipt_csv": len(engine.tgt_b)}
        self.created = time.time()
        self.state = RoundState(
            engine=engine,
            end_commit=result["end_commit"],
            intervals_a=result["waybill_csv"]["intervals"],
            intervals_b=result["receipt_csv"]["intervals"],
            changes_applied=result["changes_applied"],
            batches=0,
        )

    def view(self) -> dict:
        """当前已生效结果的一致快照(续查校验期间看到的仍是上次结果)。"""
        st = self.state
        return {
            "round_id": self.id,
            "start_commit": self.orig_start,
            "end_commit": st.end_commit,
            "compare_points": st.end_commit - self.orig_start + 1,
            "changes_applied": st.changes_applied,
            "batches": st.batches,
            "snapshot_rows": self.snapshot_rows,
            "csv_rows": self.csv_rows,
            "waybill_csv": {
                "intervals": st.intervals_a,
                "match_commits": sum(b - a + 1 for a, b in st.intervals_a),
            },
            "receipt_csv": {
                "intervals": st.intervals_b,
                "match_commits": sum(b - a + 1 for a, b in st.intervals_b),
            },
            "intersection": intersect_intervals(st.intervals_a, st.intervals_b),
        }


class RoundStore:
    """进程内轮次表,LRU 淘汰,容量 MAX_ROUNDS。"""

    def __init__(self, capacity: int = MAX_ROUNDS):
        self._items: OrderedDict[str, Round] = OrderedDict()
        self._cap = capacity
        self._lock = threading.Lock()

    def put(self, rnd: Round):
        with self._lock:
            self._items[rnd.id] = rnd
            self._items.move_to_end(rnd.id)
            while len(self._items) > self._cap:
                self._items.popitem(last=False)

    def get(self, rid: str):
        with self._lock:
            rnd = self._items.get(rid)
            if rnd is not None:
                self._items.move_to_end(rid)
            return rnd


def apply_continuation(rnd: Round, data):
    """解析并应用一个续查包。返回 (status, message, result|None)。

    status:
      ok / no_common_source  整包通过,结果已更新(result 为最新视图)
      insufficient_evidence  缺号或前值不符,整批退回,原状态全部保留
      stale_endpoint         起点不等于当前终点(终点已变),result 为当前视图
      invalid_input          续查包格式错误
    """
    try:
        cont = parse_continuation(data)
    except InputError as e:
        return "invalid_input", str(e), None

    with rnd.lock:
        cur = rnd.state
        if cont.start_commit != cur.end_commit:
            return ("stale_endpoint",
                    f"终点已变:续包起点为 {cont.start_commit},当前终点为 "
                    f"{cur.end_commit},请按当前终点重新组织续包",
                    rnd.view())
        eng2 = cur.engine.clone()
        try:
            pts_a, pts_b = replay_continuation(eng2, cont)
        except EvidenceError as e:
            return ("insufficient_evidence",
                    f"整批退回:{e}。原终点、两表状态与结果均保留,"
                    f"改正后可从原终点 {cur.end_commit} 重传",
                    None)
        rnd.state = RoundState(
            engine=eng2,
            end_commit=cont.end_commit,
            intervals_a=merge_points_into_intervals(cur.intervals_a, pts_a),
            intervals_b=merge_points_into_intervals(cur.intervals_b, pts_b),
            changes_applied=cur.changes_applied + cont.total_changes,
            batches=cur.batches + 1,
        )
        view = rnd.view()
        if view["intersection"]:
            return "ok", "续查通过,结果已更新", view
        return "no_common_source", "续查通过,但累计吻合区间交集为空", view
