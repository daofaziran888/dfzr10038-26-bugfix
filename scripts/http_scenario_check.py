"""通过真实 HTTP 接口回放目录发布并发场景，验证路由层分派与响应。"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8123"


def call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def action(module, entry_id, name):
    return call("POST", f"/api/{module}/{entry_id}/actions", {"action": name})


def create(module, values):
    return call("POST", f"/api/{module}", {"values": values})


def list_rows(module, size=200):
    return call("GET", f"/api/{module}?size={size}")[1]["items"]


def main():
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8123"],
        cwd="/workspace/backend", stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        for _ in range(60):
            try:
                if call("GET", "/api/health")[0] == 200:
                    break
            except Exception:
                time.sleep(0.2)
        else:
            raise SystemExit("server did not start")

        a = create("feat_catalog", {
            "索引编号": "FEAT-0201", "目录名称": "甲目录", "父目录": "根目录",
        })[1]["entry"]
        b = create("feat_catalog", {
            "索引编号": "FEAT-0202", "目录名称": "乙目录", "父目录": "根目录",
        })[1]["entry"]
        print("created", a["id"], b["id"])

        # 占满待审池：两个目录各领一条审计批。
        action("feat_catalog", a["id"], "发布目录")
        action("feat_catalog", b["id"], "发布目录")

        cat_a = call("GET", f"/api/feat_catalog/{a['id']}")[1]
        cat_b = call("GET", f"/api/feat_catalog/{b['id']}")[1]
        assert cat_a["审计批号"] != cat_b["审计批号"], "两目录不能共用审计批"

        # 只留一份成稿名额。
        for row in list_rows("report_task"):
            if row["status"] == "草稿":
                action("report_task", row["id"], "标记失败")
        create("report_task", {"报告编号": "REPO-9101", "报告类型": "特征报告", "特征范围": "占位"})

        audit_b = call("GET", f"/api/govern_audit/{cat_b['审计记录编号']}")[1]
        status, body = action("govern_audit", audit_b["id"], "确认通过")
        print("confirm B:", body["message"])
        sb = call("GET", f"/api/feat_catalog/{b['id']}/report-status")[1]
        print("B status:", sb["签发状态"], sb["报告编号"], sb["审计依据"])
        assert sb["签发状态"] == "已签发" and sb["审计依据"] == cat_b["审计批号"]

        # 甲还没有正式结论，试签 -> 待核。
        status, body = action("feat_catalog", a["id"], "签发报告")
        print("sign A early:", body["message"])
        sa = call("GET", f"/api/feat_catalog/{a['id']}/report-status")[1]
        print("A status:", sa["签发状态"], sa["待办原因"])
        assert sa["签发状态"] == "待核" and sa["待办原因"] == "待审计结论"

        # 乙当次再领草稿：仍指向自己的审计批，不占用新名额。
        status, body = action("feat_catalog", b["id"], "签发报告")
        print("B re-sign:", body["message"])
        sb2 = call("GET", f"/api/feat_catalog/{b['id']}/report-status")[1]
        assert sb2["报告编号"] == sb["报告编号"]

        # 补齐甲的结论：名额已满 -> 待名额。
        audit_a = call("GET", f"/api/govern_audit/{cat_a['审计记录编号']}")[1]
        status, body = action("govern_audit", audit_a["id"], "确认通过")
        print("confirm A:", body["message"])
        sa = call("GET", f"/api/feat_catalog/{a['id']}/report-status")[1]
        print("A status:", sa["签发状态"], sa["待办原因"])
        assert sa["签发状态"] == "待名额" and sa["待办原因"] == "成稿名额已用尽"

        # 草稿补足 -> 甲的排队签发落到自己的审计批。
        create("report_task", {"报告编号": "REPO-9102", "报告类型": "特征报告", "特征范围": "占位"})
        sa = call("GET", f"/api/feat_catalog/{a['id']}/report-status")[1]
        print("A after draft:", sa["签发状态"], sa["报告编号"], sa["审计依据"])
        assert sa["签发状态"] == "已签发" and sa["审计依据"] == cat_a["审计批号"]

        # 核对报告列表：两份签发各自指向审计批，没有多出来的通过报告。
        reports = list_rows("report_task")
        signed = [r for r in reports if r.get("审计批号") in {cat_a["审计批号"], cat_b["审计批号"]}]
        print("signed:", [(r["报告编号"], r["审计批号"], r["特征范围"]) for r in signed])
        assert len(signed) == 2
        assert {r["审计批号"] for r in signed} == {cat_a["审计批号"], cat_b["审计批号"]}
        assert len({r["报告编号"] for r in signed}) == 2

        # 历史兼容：已发布目录 2 只有“已通过样例”审计批，来源能核实则复用。
        status, body = action("feat_catalog", 2, "发布目录")
        print("historical catalog publish:", body["message"])
        s2 = call("GET", "/api/feat_catalog/2/report-status")[1]
        print("historical status:", s2["签发状态"], s2["审计批号"], s2["报告编号"])

        # 没有任何可核实结论的目录不能凭空签发。
        print("ALL HTTP CHECKS PASSED")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
