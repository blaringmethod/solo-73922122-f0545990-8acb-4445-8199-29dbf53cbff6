"""自测:核心场景、证据不足分支、增量计数一致性、5 万条变更性能、
续查接力/整批退回/并发互斥、HTTP 接口。

运行:python3 -m tests.selftest   (在仓库根目录)
"""
import json
import random
import sys
import threading
import time

from app.core import (
    EvidenceError,
    InputError,
    load_receipt_csv,
    load_waybill_csv,
    parse_continuation,
    parse_evidence,
    verify,
    verify_initial,
)
from app.rounds import Round, apply_continuation
from app.sample import build_continuations, build_sample

FAILED = []


def check(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {extra}" if extra else ""))
    if not cond:
        FAILED.append(name)


def expect_evidence_error(name, fn, needle):
    try:
        fn()
    except EvidenceError as e:
        check(name, needle in str(e), f"-> {e}")
        return
    except Exception as e:  # noqa: BLE001
        check(name, False, f"异常类型不对: {type(e).__name__}: {e}")
        return
    check(name, False, "未抛出 EvidenceError")


def run_verify(evidence_dict, wb_csv, rc_csv):
    ev = parse_evidence(json.loads(json.dumps(evidence_dict)))
    return verify(ev, load_waybill_csv(wb_csv.encode()), load_receipt_csv(rc_csv.encode()))


def make_round():
    """用样例初查建立一个轮次(终点 110)。"""
    evidence, wb_csv, rc_csv, _ = build_sample()
    ev = parse_evidence(json.loads(json.dumps(evidence)))
    eng, result = verify_initial(ev, load_waybill_csv(wb_csv.encode()),
                                 load_receipt_csv(rc_csv.encode()))
    return Round(eng, ev.start_commit, result["snapshot_rows"], result)


def t_sample():
    print("== 样例场景(提交 100..110)")
    from app.core import to_intervals
    evidence, wb_csv, rc_csv, expected = build_sample()
    r = run_verify(evidence, wb_csv, rc_csv)
    check("运单 CSV 吻合区间",
          r["waybill_csv"]["intervals"] == to_intervals(expected["waybill_points"]),
          str(r["waybill_csv"]["intervals"]))
    check("签收 CSV 吻合区间",
          r["receipt_csv"]["intervals"] == to_intervals(expected["receipt_points"]),
          str(r["receipt_csv"]["intervals"]))
    check("交集", r["intersection"] == expected["intersection"], str(r["intersection"]))
    check("变更条数", r["changes_applied"] == 12, str(r["changes_applied"]))


def t_counter_consistency():
    print("== 增量计数 vs 全量重算(逐提交核对)")
    from app.core import Engine, parse_evidence as pe
    evidence, wb_csv, rc_csv, _ = build_sample()
    ev = pe(json.loads(json.dumps(evidence)))
    eng = Engine(load_waybill_csv(wb_csv.encode()), load_receipt_csv(rc_csv.encode()))
    eng.load_snapshot(ev.wb_rows, ev.rc_rows)
    ok = (eng.mism_a, eng.mism_b) == eng.full_mismatches()
    for tx in ev.transactions:
        for i, ch in enumerate(tx.changes):
            eng.apply_change(ch, f"t{tx.commit}#{i}")
        ok = ok and (eng.mism_a, eng.mism_b) == eng.full_mismatches()
    check("每个提交后增量计数与全量重算一致", ok)


def t_no_common_source():
    print("== 交集为空 -> 所查范围内没有共同来源")
    evidence = {
        "start_commit": 200, "end_commit": 201,
        "snapshots": {
            "waybills": [{"单号": "W1", "状态": "已签收", "件数": 3}],
            "receipts": [{"签收号": "R1", "单号": "W1", "实收件数": 3}],
        },
        "transactions": [{"commit": 201, "changes": [
            {"table": "waybills", "op": "update", "key": "W1",
             "before": {"单号": "W1", "状态": "已签收", "件数": 3},
             "after": {"单号": "W1", "状态": "已签收", "件数": 4}},
            {"table": "receipts", "op": "update", "key": "R1",
             "before": {"签收号": "R1", "单号": "W1", "实收件数": 3},
             "after": {"签收号": "R1", "单号": "W1", "实收件数": 4}},
        ]}],
    }
    # 运单 CSV 只在 200 吻合;签收 CSV 只在 201 吻合
    r = run_verify(evidence, "单号,状态,件数\nW1,已签收,3\n", "签收号,单号,实收件数\nR1,W1,4\n")
    check("运单吻合点=[200]", r["waybill_csv"]["intervals"] == [[200, 200]], str(r["waybill_csv"]["intervals"]))
    check("签收吻合点=[201]", r["receipt_csv"]["intervals"] == [[201, 201]], str(r["receipt_csv"]["intervals"]))
    check("交集为空", r["intersection"] == [])


def t_evidence_errors():
    print("== 证据不足分支")
    evidence, wb_csv, rc_csv, _ = build_sample()

    gap = json.loads(json.dumps(evidence))
    gap["transactions"] = [t for t in gap["transactions"] if t["commit"] != 105]
    expect_evidence_error("日志缺号(中间缺 105)",
                          lambda: run_verify(gap, wb_csv, rc_csv), "日志缺号")

    trunc = json.loads(json.dumps(evidence))
    trunc["transactions"] = trunc["transactions"][:-2]  # 只到 108,end 仍为 110
    expect_evidence_error("日志缺号(尾部缺失)",
                          lambda: run_verify(trunc, wb_csv, rc_csv), "日志缺号")

    bad_before = json.loads(json.dumps(evidence))
    bad_before["transactions"][2]["changes"][0]["before"]["件数"] = 99  # 提交 103
    expect_evidence_error("前值对不上",
                          lambda: run_verify(bad_before, wb_csv, rc_csv), "前值对不上")

    beyond = json.loads(json.dumps(evidence))
    beyond["end_commit"] = 109  # 日志含 110,超出终止号
    expect_evidence_error("提交号超出终止号",
                          lambda: run_verify(beyond, wb_csv, rc_csv), "超出终止提交号")


def t_receipt_retarget():
    print("== 签收行改挂单号 + 运单状态翻转(多签收行)")
    evidence = {
        "start_commit": 300, "end_commit": 303,
        "snapshots": {
            "waybills": [{"单号": "A", "状态": "已签收", "件数": 1},
                         {"单号": "B", "状态": "运输中", "件数": 2}],
            "receipts": [{"签收号": "R1", "单号": "A", "实收件数": 1},
                         {"签收号": "R2", "单号": "A", "实收件数": 1},
                         {"签收号": "R3", "单号": "B", "实收件数": 2}],
        },
        "transactions": [
            {"commit": 301, "changes": [  # R3 改挂到已签收的 A -> 签收导出多出 R3
                {"table": "receipts", "op": "update", "key": "R3",
                 "before": {"签收号": "R3", "单号": "B", "实收件数": 2},
                 "after": {"签收号": "R3", "单号": "A", "实收件数": 2}}]},
            {"commit": 302, "changes": [  # B 变为已签收,但 B 名下已无签收行
                {"table": "waybills", "op": "update", "key": "B",
                 "before": {"单号": "B", "状态": "运输中", "件数": 2},
                 "after": {"单号": "B", "状态": "已签收", "件数": 2}}]},
            {"commit": 303, "changes": [  # 删除 R3 -> 签收导出恢复吻合
                {"table": "receipts", "op": "delete", "key": "R3",
                 "before": {"签收号": "R3", "单号": "A", "实收件数": 2}, "after": None}]},
        ],
    }
    r = run_verify(evidence, "单号,状态,件数\nA,已签收,1\n",
                   "签收号,单号,实收件数\nR1,A,1\nR2,A,1\n")
    check("运单吻合区间", r["waybill_csv"]["intervals"] == [[300, 301]],
          str(r["waybill_csv"]["intervals"]))
    check("签收吻合区间", r["receipt_csv"]["intervals"] == [[300, 300], [303, 303]],
          str(r["receipt_csv"]["intervals"]))
    check("交集=[300]", r["intersection"] == [[300, 300]], str(r["intersection"]))


def t_csv_validation():
    print("== CSV 输入校验")
    try:
        load_waybill_csv("单号,状态\nW1,已签收\n".encode())
        check("缺列报错", False)
    except InputError as e:
        check("缺列报错", "件数" in str(e), str(e))
    try:
        load_waybill_csv("单号,状态,件数\nW1,已签收,1\nW1,已签收,2\n".encode())
        check("重复键报错", False)
    except InputError as e:
        check("重复键报错", "重复" in str(e), str(e))
    d = load_waybill_csv("单号,状态,件数\nW1,已签收,3\n".encode())
    check("正常解析", d == {"W1": ("已签收", "3")}, str(d))
    d2 = load_waybill_csv(" 单号 ,状态,件数\nW1,已签收,3\n".encode())
    check("表头带空格可解析", d2 == {"W1": ("已签收", "3")}, str(d2))
    d3 = load_waybill_csv("单号,状态,件数\n".encode() and "单号,状态,件数\nW1,已签收,3\n".encode())
    check("BOM 头可解析", load_waybill_csv("单号,状态,件数\nW1,已签收,3\n".encode("utf-8-sig")) == d3)


def t_continuation_parse():
    print("== 续查包格式校验")
    cont1, _, _ = build_continuations()
    d = json.loads(json.dumps(cont1))
    d["snapshots"] = {"waybills": [], "receipts": []}
    try:
        parse_continuation(d)
        check("续查包携带 snapshots 拒绝", False)
    except InputError as e:
        check("续查包携带 snapshots 拒绝", "snapshots" in str(e), str(e))
    d = json.loads(json.dumps(cont1))
    d["end_commit"] = d["start_commit"]
    try:
        parse_continuation(d)
        check("终止号不大于起始号拒绝", False)
    except InputError as e:
        check("终止号不大于起始号拒绝", "更晚" in str(e), str(e))
    d = json.loads(json.dumps(cont1))
    del d["transactions"]
    try:
        parse_continuation(d)
        check("缺 transactions 拒绝", False)
    except InputError as e:
        check("缺 transactions 拒绝", "transactions" in str(e), str(e))
    c = parse_continuation(json.loads(json.dumps(cont1)))
    check("正常解析续查包",
          c.start_commit == 110 and c.end_commit == 116 and len(c.transactions) == 6
          and c.total_changes == 8,
          f"{c.start_commit}->{c.end_commit} {len(c.transactions)}tx {c.total_changes}ch")


def t_continuation_flow():
    print("== 续查:两批接力 + 跨批区间合并")
    cont1, cont2, expected = build_continuations()
    rnd = make_round()
    v0 = rnd.view()
    check("初始终点=110,批数=0", v0["end_commit"] == 110 and v0["batches"] == 0)

    st, msg, view = apply_continuation(rnd, json.loads(json.dumps(cont1)))
    check("续查包1通过", st == "ok", f"{st}: {msg}")
    check("包1后终点=116,批数=1", view["end_commit"] == 116 and view["batches"] == 1)
    check("包1运单区间", view["waybill_csv"]["intervals"] == expected["batch1"]["waybill_intervals"],
          str(view["waybill_csv"]["intervals"]))
    check("包1签收区间", view["receipt_csv"]["intervals"] == expected["batch1"]["receipt_intervals"],
          str(view["receipt_csv"]["intervals"]))
    check("包1交集", view["intersection"] == expected["batch1"]["intersection"],
          str(view["intersection"]))
    check("跨批相邻区间合并(110-111)", [110, 111] in view["waybill_csv"]["intervals"])
    check("包1累计变更=20", view["changes_applied"] == 20, str(view["changes_applied"]))

    st, msg, view = apply_continuation(rnd, json.loads(json.dumps(cont2)))
    check("续查包2通过", st == "ok", f"{st}: {msg}")
    check("包2后终点=118,批数=2", view["end_commit"] == 118 and view["batches"] == 2)
    check("包2运单区间", view["waybill_csv"]["intervals"] == expected["batch2"]["waybill_intervals"],
          str(view["waybill_csv"]["intervals"]))
    check("包2签收区间", view["receipt_csv"]["intervals"] == expected["batch2"]["receipt_intervals"],
          str(view["receipt_csv"]["intervals"]))
    check("包2交集", view["intersection"] == expected["batch2"]["intersection"],
          str(view["intersection"]))
    check("跨批相邻区间合并(116-118)", [116, 118] in view["waybill_csv"]["intervals"]
          and [116, 118] in view["receipt_csv"]["intervals"])
    check("包2累计变更=22", view["changes_applied"] == 22, str(view["changes_applied"]))


def t_continuation_reject():
    print("== 续查整批退回:保留原终点/状态/结果,改正后可重传")
    cont1, _, expected = build_continuations()
    base_intervals = [[100, 100], [102, 102], [105, 106], [108, 108], [110, 110]]
    base_intervals_rc = [[100, 100], [102, 103], [106, 106], [108, 108], [110, 110]]
    rnd = make_round()

    gap = json.loads(json.dumps(cont1))
    gap["transactions"] = [t for t in gap["transactions"] if t["commit"] != 113]
    st, msg, view = apply_continuation(rnd, gap)
    check("缺号 -> 整批退回", st == "insufficient_evidence" and "缺号" in msg, f"{st}: {msg}")
    v = rnd.view()
    check("退回后终点保持 110", v["end_commit"] == 110)
    check("退回后批数保持 0", v["batches"] == 0)
    check("退回后运单区间不变", v["waybill_csv"]["intervals"] == base_intervals)
    check("退回后签收区间不变", v["receipt_csv"]["intervals"] == base_intervals_rc)

    bad = json.loads(json.dumps(cont1))
    bad["transactions"][0]["changes"][0]["before"]["实收件数"] = 99
    st, msg, view = apply_continuation(rnd, bad)
    check("前值不符 -> 整批退回", st == "insufficient_evidence" and "前值" in msg, f"{st}: {msg}")
    check("退回后终点仍保持 110", rnd.view()["end_commit"] == 110)

    st, msg, view = apply_continuation(rnd, json.loads(json.dumps(cont1)))
    check("改正后从原终点重传通过", st == "ok" and view["end_commit"] == 116, f"{st}: {msg}")
    check("重传后区间正确", view["waybill_csv"]["intervals"] == expected["batch1"]["waybill_intervals"])

    st, msg, view = apply_continuation(rnd, json.loads(json.dumps(cont1)))
    check("旧起点续包 -> 终点已变", st == "stale_endpoint" and "终点已变" in msg, f"{st}: {msg}")
    check("终点已变时返回当前视图", view is not None and view["end_commit"] == 116)


def t_engine_clone_isolation():
    print("== 引擎克隆隔离:续查失败不污染原状态")
    from app.core import parse_continuation as pc, replay_continuation
    cont1, _, _ = build_continuations()
    rnd = make_round()
    eng = rnd.state.engine
    before = (sorted(eng.waybills), sorted(eng.receipts), eng.mism_a, eng.mism_b)
    bad = json.loads(json.dumps(cont1))
    bad["transactions"][2]["changes"][0]["before"]["实收件数"] = 99  # 提交 113 前值错
    eng2 = eng.clone()
    try:
        replay_continuation(eng2, pc(bad))
        check("克隆上重放抛 EvidenceError", False)
    except EvidenceError:
        check("克隆上重放抛 EvidenceError", True)
    after = (sorted(eng.waybills), sorted(eng.receipts), eng.mism_a, eng.mism_b)
    check("原引擎状态未被污染", before == after)
    ok = eng.clone()
    pts_a, pts_b = replay_continuation(ok, pc(json.loads(json.dumps(cont1))))
    check("克隆上完整重放成功", pts_a == [111, 114, 116] and pts_b == [113, 114, 116],
          f"a={pts_a} b={pts_b}")


def t_continuation_concurrent():
    print("== 并发续查:同一终点只生效一份")
    cont1, _, _ = build_continuations()
    rnd = make_round()
    barrier = threading.Barrier(2)
    results = []

    def worker():
        barrier.wait()
        results.append(apply_continuation(rnd, json.loads(json.dumps(cont1)))[0])

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("一份生效、另一份提示终点已变",
          sorted(results) == ["ok", "stale_endpoint"], str(results))
    v = rnd.view()
    check("终点只推进一次", v["end_commit"] == 116 and v["batches"] == 1,
          f"end={v['end_commit']} batches={v['batches']}")


def t_perf_50k():
    print("== 5 万条变更性能与一致性")
    rng = random.Random(42)
    n_wb, n_rc = 5000, 5000
    wb0 = {f"W{i:05d}": {"单号": f"W{i:05d}", "状态": "已签收" if i % 2 == 0 else "运输中",
                         "件数": rng.randint(1, 9)} for i in range(n_wb)}
    rc0 = {f"R{i:05d}": {"签收号": f"R{i:05d}", "单号": f"W{(i * 7) % n_wb:05d}",
                         "实收件数": rng.randint(1, 9)} for i in range(n_rc)}
    # 工作副本:生成变更日志的同时推进,保证 before 值与重放一致
    wb = {k: dict(v) for k, v in wb0.items()}
    rc = {k: dict(v) for k, v in rc0.items()}
    n_tx, per_tx = 500, 100  # 共 50000 条变更
    transactions = []
    for c in range(1, n_tx + 1):
        changes = []
        for _ in range(per_tx):
            if rng.random() < 0.5:
                k = f"W{rng.randrange(n_wb):05d}"
                before = dict(wb[k])
                after = dict(before)
                if rng.random() < 0.3:
                    after["状态"] = "运输中" if before["状态"] == "已签收" else "已签收"
                else:
                    after["件数"] = rng.randint(1, 9)
                wb[k] = after
                changes.append({"table": "waybills", "op": "update", "key": k,
                                "before": before, "after": after})
            else:
                k = f"R{rng.randrange(n_rc):05d}"
                before = dict(rc[k])
                after = dict(before)
                after["实收件数"] = rng.randint(1, 9)
                rc[k] = after
                changes.append({"table": "receipts", "op": "update", "key": k,
                                "before": before, "after": after})
        transactions.append({"commit": 1000 + c, "changes": changes})
    evidence = {"start_commit": 1000, "end_commit": 1000 + n_tx,
                "snapshots": {"waybills": list(wb0.values()), "receipts": list(rc0.values())},
                "transactions": transactions}

    t0 = time.perf_counter()
    ev = parse_evidence(json.loads(json.dumps(evidence)))
    # 目标取终点状态:最后一个比对点必吻合
    tgt_a = {k: (r["状态"], str(r["件数"])) for k, r in wb.items() if r["状态"] == "已签收"}
    tgt_b = {k: (r["单号"], str(r["实收件数"])) for k, r in rc.items()
             if r["单号"] in tgt_a}
    r = verify(ev, tgt_a, tgt_b)
    dt = time.perf_counter() - t0
    check("变更条数=50000", r["changes_applied"] == 50000, str(r["changes_applied"]))
    check("终点吻合(目标=终点导出)",
          r["waybill_csv"]["intervals"][-1][1] == 1000 + n_tx
          and r["receipt_csv"]["intervals"][-1][1] == 1000 + n_tx,
          f"运单末区间={r['waybill_csv']['intervals'][-1]} 签收末区间={r['receipt_csv']['intervals'][-1]}")
    print(f"  耗时 {dt:.2f}s(解析+重放 50000 条变更, 快照 10000 行)")
    check("性能可接受(<20s)", dt < 20, f"{dt:.2f}s")


def t_http():
    print("== HTTP 接口(TestClient)")
    try:
        from fastapi.testclient import TestClient
        from app.main import app
    except Exception as e:  # noqa: BLE001
        print(f"  [SKIP] fastapi 不可用: {e}")
        return
    client = TestClient(app)
    evidence, wb_csv, rc_csv, expected = build_sample()
    files = {
        "waybill_csv": ("waybills.csv", wb_csv.encode(), "text/csv"),
        "receipt_csv": ("receipts.csv", rc_csv.encode(), "text/csv"),
        "evidence": ("evidence.json", json.dumps(evidence, ensure_ascii=False).encode(),
                     "application/json"),
    }
    resp = client.post("/api/verify", files=files)
    check("样例核查 HTTP 200", resp.status_code == 200, str(resp.status_code))
    body = resp.json()
    check("样例核查结论 ok", body.get("status") == "ok", str(body.get("status")))
    check("样例交集正确", body["result"]["intersection"] == expected["intersection"],
          str(body["result"]["intersection"]))

    bad = json.loads(json.dumps(evidence))
    bad["transactions"] = [t for t in bad["transactions"] if t["commit"] != 107]
    files2 = dict(files)
    files2["evidence"] = ("evidence.json", json.dumps(bad, ensure_ascii=False).encode(),
                          "application/json")
    body2 = client.post("/api/verify", files=files2).json()
    check("缺号 -> insufficient_evidence", body2.get("status") == "insufficient_evidence",
          str(body2.get("status")))

    files3 = dict(files)
    files3["waybill_csv"] = ("waybills.csv", "单号,状态\nW1,已签收\n".encode(), "text/csv")
    resp3 = client.post("/api/verify", files=files3)
    check("坏 CSV -> 400 invalid_input",
          resp3.status_code == 400 and resp3.json().get("status") == "invalid_input",
          f"{resp3.status_code} {resp3.json().get('status')}")

    resp4 = client.get("/")
    check("操作页可访问", resp4.status_code == 200 and "货运数仓来源核对" in resp4.text)
    resp5 = client.get("/api/sample/evidence.json")
    check("样例证据包可下载", resp5.status_code == 200
          and resp5.json()["start_commit"] == 100)

    # ---- 续查 HTTP 流程 ----
    print("== HTTP 续查流程")
    rid = body["round_id"]
    check("初查返回本轮链接", body.get("round_url") == f"/round/{rid}", str(body.get("round_url")))
    resp = client.get(body["round_url"])
    check("轮次页可打开", resp.status_code == 200 and "续查" in resp.text, str(resp.status_code))
    resp = client.get(f"/api/round/{rid}")
    check("轮次状态接口", resp.status_code == 200
          and resp.json()["result"]["end_commit"] == 110)

    cont1, cont2, cexpected = build_continuations()

    def post_cont(rid_, obj):
        return client.post(f"/api/round/{rid_}/continue", files={
            "evidence": ("cont.json", json.dumps(obj, ensure_ascii=False).encode(),
                         "application/json")})

    resp = post_cont(rid, cont1)
    b = resp.json()
    check("续查包1 HTTP 200 ok", resp.status_code == 200 and b["status"] == "ok",
          f"{resp.status_code} {b.get('status')}")
    check("续查包1区间合并", b["result"]["waybill_csv"]["intervals"]
          == cexpected["batch1"]["waybill_intervals"], str(b["result"]["waybill_csv"]["intervals"]))
    check("续查包1交集", b["result"]["intersection"] == cexpected["batch1"]["intersection"])

    resp = post_cont(rid, cont2)
    b = resp.json()
    check("续查包2 HTTP 200 ok", resp.status_code == 200 and b["status"] == "ok",
          f"{resp.status_code} {b.get('status')}")
    check("跨批合并 116-118", [116, 118] in b["result"]["waybill_csv"]["intervals"],
          str(b["result"]["waybill_csv"]["intervals"]))
    check("续查包2交集", b["result"]["intersection"] == cexpected["batch2"]["intersection"])

    resp = post_cont(rid, cont1)  # 旧起点 110,当前终点已变为 118
    check("旧起点续包 -> 409 终点已变",
          resp.status_code == 409 and resp.json()["status"] == "stale_endpoint",
          f"{resp.status_code} {resp.json().get('status')}")
    check("终点已变响应带当前视图", resp.json()["result"]["end_commit"] == 118)

    gap = {"start_commit": 118, "end_commit": 119, "transactions": []}
    resp = post_cont(rid, gap)
    check("缺号 -> 整批退回", resp.status_code == 200
          and resp.json()["status"] == "insufficient_evidence",
          f"{resp.status_code} {resp.json().get('status')}")
    resp = client.get(f"/api/round/{rid}")
    check("退回后终点保持 118", resp.json()["result"]["end_commit"] == 118)
    check("退回后批数保持 2", resp.json()["result"]["batches"] == 2)

    bad_json = post_cont(rid, {"start_commit": 118, "end_commit": 120,
                               "transactions": "not-a-list"})
    check("坏续查包 -> 400 invalid_input",
          bad_json.status_code == 400 and bad_json.json()["status"] == "invalid_input",
          f"{bad_json.status_code}")

    resp = client.get("/round/deadbeef")
    check("未知轮次页 404", resp.status_code == 404, str(resp.status_code))
    resp = post_cont("deadbeef", cont1)
    check("未知轮次续查 404", resp.status_code == 404
          and resp.json()["status"] == "not_found", str(resp.status_code))

    resp = client.get("/api/sample/continuation1.json")
    check("样例续查包1可下载", resp.status_code == 200 and resp.json()["start_commit"] == 110)
    resp = client.get("/api/sample/continuation2.json")
    check("样例续查包2可下载", resp.status_code == 200 and resp.json()["start_commit"] == 116)


def main():
    t_sample()
    t_counter_consistency()
    t_no_common_source()
    t_evidence_errors()
    t_receipt_retarget()
    t_csv_validation()
    t_continuation_parse()
    t_continuation_flow()
    t_continuation_reject()
    t_engine_clone_isolation()
    t_continuation_concurrent()
    t_perf_50k()
    t_http()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {', '.join(FAILED)}")
        sys.exit(1)
    print("全部自测通过。")


if __name__ == "__main__":
    main()
