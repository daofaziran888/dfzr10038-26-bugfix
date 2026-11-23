"""历史兼容验证：已有通过审计批/历史报告时可核实来源的复用与补关联。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, "/workspace/backend")

from app.seed import SEED_ROWS
import app.store as store_module
from app.services.catalog_report import catalog_report_status
from app.services.feat_catalog import FeatCatalogService


def fresh():
    store_module.store._tables = {k: [dict(r) for r in v] for k, v in SEED_ROWS.items()}


def main():
    fresh()
    catalogs = FeatCatalogService()

    # 目录 2「用户画像特征集」历史上已发布，池里有一条对得上名称/对象的通过审计批。
    audit_rows = store_module.store.rows("govern_audit")
    for row in audit_rows:
        if row["记录编号"] == "GOVE-0003":
            row["检查结果"] = "通过"
            row["审计结论"] = "通过"
    # 把这条通过批改写成属于目录 2（历史按名称记录，无索引编号）。
    for row in audit_rows:
        if row["记录编号"] == "GOVE-0003":
            row["审计对象"] = "用户画像特征集"
            row["审计类型"] = "目录发布审计"

    # 历史已生成报告，特征范围对得上目录名但没有审计关联。
    reports = store_module.store.rows("report_task")
    for row in reports:
        if row["报告编号"] == "REPO-0003":
            row["特征范围"] = "用户画像特征集"

    catalog = store_module.store.find("feat_catalog", 2)
    before_reports = len(reports)
    _, msg = catalogs.run_action(2, "发布目录")
    print("publish historical:", msg)
    status = catalog_report_status(catalog)
    print("status:", status["签发状态"], status["审计批号"], status["报告编号"], status["审计依据"])
    assert status["签发状态"] == "已签发"
    assert status["审计批号"] == "GOVE-0003"
    assert status["报告编号"] == "REPO-0003", "应复用历史可核实报告"
    assert len(store_module.store.rows("report_task")) == before_reports, "不应新占成稿名额"
    reused = store_module.store.find("report_task", 3)
    assert reused["审计批号"] == "GOVE-0003"
    assert reused["目录索引编号"] == "FEAT-0002"
    assert reused["特征范围"] == "用户画像特征集"
    print("historical reuse ok:", reused["报告编号"], reused["审计批号"])

    # 没有通过审计批的目录：发布后只有待审计材料，不产出通过报告。
    fresh()
    _, msg = catalogs.run_action(1, "发布目录")
    print("no conclusion publish:", msg)
    status = catalog_report_status(store_module.store.find("feat_catalog", 1))
    assert status["签发状态"] == "待核" and status["报告编号"] == ""
    print("ALL HISTORY CHECKS PASSED")


if __name__ == "__main__":
    main()
