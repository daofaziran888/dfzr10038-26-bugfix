"""并发验证：待审池只剩一个空位 / 成稿名额只剩一个时的抢占结果。"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "http://127.0.0.1:8124"


def call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def action(module, entry_id, name):
    return call("POST", f"/api/{module}/{entry_id}/actions", {"action": name})


def create(module, values):
    return call("POST", f"/api/{module}", {"values": values})[1]["entry"]


def rows(module):
    return call("GET", f"/api/{module}?size=200")[1]["items"]


def main():
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8124"],
        cwd="/workspace/backend", stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        for _ in range(60):
            try:
                if call("GET", "/api/health")[0] == 200:
                    break
            except Exception:
                time.sleep(0.2)

        # --- 场景1：待审池只有一个空位，三个目录并发发布。 ---
        cats = [
            create("feat_catalog", {"索引编号": f"FEAT-03{i}", "目录名称": f"并发目录{i}", "父目录": "根目录"})
            for i in range(1, 4)
        ]
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda c: action("feat_catalog", c["id"], "发布目录"), cats))
        bound = []
        for c in cats:
            row = call("GET", f"/api/feat_catalog/{c['id']}")[1]
            bound.append(row["审计批号"])
            print("catalog", c["id"], "audit", row["审计批号"], row["审计状态"])
        assert len(set(bound)) == 3, f"审计批被重复占用: {bound}"

        pending = [r for r in rows("govern_audit") if r["status"] in ("待审计", "审计中")]
        passed = [r for r in rows("govern_audit") if r["status"] == "已通过"]
        print("audit pool 待审计/审计中:", len(pending), "已通过:", len(passed))

        # 三个目录此时都不应出现引用新审计批的“通过报告”。
        new_audit_ids = {
            call("GET", f"/api/feat_catalog/{c['id']}")[1]["审计记录编号"] for c in cats
        }
        leaked = [
            r for r in rows("report_task")
            if r["status"] == "已生成" and r.get("审计记录编号") in new_audit_ids
        ]
        assert leaked == [], f"待审阶段出现了通过报告: {leaked}"

        # --- 场景2：三份通过结论同时到，只剩一个成稿名额。 ---
        create("report_task", {"报告编号": "REPO-9201", "报告类型": "特征报告", "特征范围": "占位"})
        # 清掉示例里其它草稿
        for r in rows("report_task"):
            if r["status"] == "草稿" and r["报告编号"] != "REPO-9201":
                action("report_task", r["id"], "标记失败")
        audits = []
        for c in cats:
            row = call("GET", f"/api/feat_catalog/{c['id']}")[1]
            audits.append(row["审计记录编号"])
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda aid: action("govern_audit", aid, "确认通过"), audits))

        issued = []
        for c in cats:
            st = call("GET", f"/api/feat_catalog/{c['id']}/report-status")[1]
            issued.append((st["索引编号"], st["签发状态"], st["报告编号"], st["审计依据"]))
        print("concurrent results:", issued)
        assert sum(1 for i in issued if i[1] == "已签发") == 1
        for entry in issued:
            if entry[1] == "已签发":
                assert entry[3] is not None
            else:
                assert entry[1] == "待名额" and entry[2] == ""

        signed = [
            r for r in rows("report_task")
            if r["status"] == "已生成" and r.get("审计记录编号") in new_audit_ids
        ]
        print("generated reports:", [(r["报告编号"], r.get("审计批号"), r.get("特征范围")) for r in signed])
        assert len(signed) == 1
        assert signed[0]["审计批号"] == next(i[3] for i in issued if i[1] == "已签发")

        # 草稿补足后排队签发，各自指向自己的审计批。
        create("report_task", {"报告编号": "REPO-9202", "报告类型": "特征报告", "特征范围": "占位"})
        create("report_task", {"报告编号": "REPO-9203", "报告类型": "特征报告", "特征范围": "占位"})
        final = []
        for c in cats:
            st = call("GET", f"/api/feat_catalog/{c['id']}/report-status")[1]
            final.append(st)
        print("after restock:", [(f["索引编号"], f["签发状态"], f["审计依据"]) for f in final])
        assert all(f["签发状态"] == "已签发" for f in final)
        assert len({f["报告编号"] for f in final}) == 3
        assert len({f["审计依据"] for f in final}) == 3
        print("ALL CONCURRENCY CHECKS PASSED")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
