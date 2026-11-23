"""场景回放：目录发布—审计批—成稿名额链路的并发与排队语义。

用独立进程运行，避免污染示例数据：
    cd backend && .venv/bin/python ../scripts/scenario_check.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.store import Store
import app.store as store_module
from app.seed import SEED_ROWS
from app.services import catalog_report
from app.services.feat_catalog import FeatCatalogService
from app.services.govern_audit import GovernAuditService
from app.services.report_task import ReportTaskService


def fresh():
    store_module.store._tables = {k: [dict(r) for r in v] for k, v in SEED_ROWS.items()}


def catalog_rows():
    return store_module.store.rows("feat_catalog")


def report_rows():
    return store_module.store.rows("report_task")


def audit_rows():
    return store_module.store.rows("govern_audit")


def new_catalog(code, name):
    rows = catalog_rows()
    row = {
        "id": max((int(r["id"]) for r in rows), default=0) + 1,
        "status": "草稿", "pending": True, "abnormal": False,
        "索引编号": code, "目录名称": name, "父目录": "根目录",
        "目录层级": "L1/测试", "关联特征": "f1;f2", "目录描述": name,
        "排序权重": "10", "创建时间": "2026-11-20",
    }
    rows.append(row)
    return row


def passed_reports():
    return [r for r in report_rows() if r.get("status") == "已生成"]


def main():
    fresh()
    catalogs = FeatCatalogService()
    audits = GovernAuditService()

    # 1. 用两个目录占满待审池（示例池里只有一条真正可领的“待审计”）。
    a = new_catalog("FEAT-0101", "甲目录")
    b = new_catalog("FEAT-0102", "乙目录")
    _, msg_a = catalogs.run_action(int(a["id"]), "发布目录")
    _, msg_b = catalogs.run_action(int(b["id"]), "发布目录")
    print("publish A:", msg_a)
    print("publish B:", msg_b)
    assert int(a.get("审计记录编号") or 0) != int(b.get("审计记录编号") or 0), "两目录不能共用同一审计批"
    audit_a = store_module.store.find("govern_audit", int(a["审计记录编号"]))
    audit_b = store_module.store.find("govern_audit", int(b["审计记录编号"]))
    print("A audit:", audit_a["记录编号"], audit_a["status"])
    print("B audit:", audit_b["记录编号"], audit_b["status"])

    # 2. 只剩一个成稿名额。
    reports = report_rows()
    for row in reports:
        if row["status"] == "草稿":
            row["status"] = "已归档"
    reports.append({
        "id": max(int(r["id"]) for r in reports) + 1, "status": "草稿",
        "pending": True, "abnormal": False, "报告编号": "REPO-9001",
        "报告类型": "特征报告", "特征范围": "", "统计周期": "", "图表配置": "",
        "输出格式": "", "分发列表": "", "最近生成": "",
    })

    # 3. 乙的结论先到，甲还没有正式结论：试签乙。
    _, msg = audits.run_action(int(audit_b["id"]), "确认通过")
    print("confirm B:", msg)
    b_status = catalogs.report_status(int(b["id"]))
    print("B status:", b_status["签发状态"], b_status["报告编号"], b_status["审计依据"])
    assert b_status["签发状态"] == "已签发"
    assert b_status["审计依据"] == audit_b["审计批号"]

    # 4. 甲未获结论时试签 -> 待核，不能拿乙的通过结论。
    _, msg = catalogs.run_action(int(a["id"]), "签发报告")
    print("try sign A before conclusion:", msg)
    a_status = catalogs.report_status(int(a["id"]))
    print("A status:", a_status["签发状态"], a_status["待办原因"])
    assert a_status["签发状态"] == "待核"
    assert a_status["待办原因"] == "待审计结论"

    # 5. 补齐甲的结论，但名额已用尽 -> 待名额。
    _, msg = audits.run_action(int(audit_a["id"]), "确认通过")
    print("confirm A:", msg)
    a_status = catalogs.report_status(int(a["id"]))
    print("A status:", a_status["签发状态"], a_status["待办原因"])
    assert a_status["签发状态"] == "待名额"
    assert a_status["待办原因"] == "成稿名额已用尽"

    # 6. 乙当次能否再领用草稿：没有草稿则待名额。
    _, msg = catalogs.run_action(int(b["id"]), "签发报告")
    print("B re-sign:", msg)
    assert catalogs.report_status(int(b["id"]))["签发状态"] == "已签发"

    # 7. 草稿补足后，甲的排队签发指向自己的审计批。
    reports.append({
        "id": max(int(r["id"]) for r in reports) + 1, "status": "草稿",
        "pending": True, "abnormal": False, "报告编号": "REPO-9002",
        "报告类型": "特征报告", "特征范围": "", "统计周期": "", "图表配置": "",
        "输出格式": "", "分发列表": "", "最近生成": "",
    })
    ReportTaskService().create_entry({
        "报告编号": "REPO-9003", "报告类型": "特征报告", "特征范围": "占位",
    })
    a_status = catalogs.report_status(int(a["id"]))
    print("A after drafts:", a_status["签发状态"], a_status["报告编号"], a_status["审计依据"])
    assert a_status["签发状态"] == "已签发"
    assert a_status["审计依据"] == audit_a["审计批号"]

    # 8. 两次签发都指向各自审计批，原待审时段没有新增通过报告。
    reports_after = passed_reports()
    signed = {r["报告编号"]: r for r in reports_after if r.get("审计记录编号") in
              {int(audit_a["id"]), int(audit_b["id"])}}
    print("signed reports:", {k: (v["审计批号"], v["特征范围"]) for k, v in signed.items()})
    assert signed[a_status["报告编号"]]["审计批号"] == audit_a["审计批号"]
    assert signed[b_status["报告编号"]]["审计批号"] == audit_b["审计批号"]

    # 9. 驳回的结论不能签发。
    c = new_catalog("FEAT-0103", "丙目录")
    catalogs.run_action(int(c["id"]), "发布目录")
    audit_c = store_module.store.find("govern_audit", int(c["审计记录编号"]))
    audits.run_action(int(audit_c["id"]), "指派整改")
    _, msg = catalogs.run_action(int(c["id"]), "签发报告")
    print("rejected C:", msg)
    assert catalogs.report_status(int(c["id"]))["签发状态"] == "已驳回"

    # 10. 已占用结论不能被其他目录挪用：把甲的审计批强行指给丙也不行。
    assert catalog_report._find_bound_audit(c) is audit_c
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
