"""特征报告领域服务：生成/签发必须读取通过审计的目录数据与真实审计批。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.catalog_report import (
    audit_batch_no,
    complete_report,
    is_passed,
    process_pending_issuances,
    resolve_catalog_for_report,
)

# 报告自身的额外关联字段，登记时允许一并写入（例如给某个目录预留草稿）。
EXTRA_FIELDS = ["目录索引编号", "审计记录编号", "审计批号", "审计依据", "签发占用目录"]


class ReportTaskService(BaseModuleService):
    MODULE = "report_task"
    ENTITY = "分析报表"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["报告编号", "报告类型", "特征范围", "统计周期", "图表配置", "输出格式", "分发列表", "最近生成",
                   "审计批号", "审计依据"]
    REQUIRED_FIELDS = ["报告编号", "报告类型", "特征范围"]
    STATUS_ORDER = ["草稿", "生成中", "已生成", "生成失败"]
    ACTION_RULES = {"启动生成": "生成中", "中止生成": "已生成", "标记失败": "生成失败"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is None:
            return entry, missing
        for field in EXTRA_FIELDS:
            if values.get(field) is not None and str(values.get(field)).strip():
                entry[field] = values.get(field)
        # 草稿补足后，把排队中的目录按先后顺序继续签发。
        if entry.get("status") == "草稿":
            process_pending_issuances()
        return entry, missing

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action in {"启动生成", "中止生成", "签发报告", "确认生成"} or "签发" in action:
            catalog = resolve_catalog_for_report(entry)
            if catalog is None:
                # 与目录无关的普通报表仍按原状态机流转，不会产出“目录通过报告”。
                return super().run_action(entry_id, action)
            return self._complete(entry, action)

        return super().run_action(entry_id, action)

    def _complete(
        self, report: dict[str, Any], action: str
    ) -> tuple[dict[str, Any] | None, str]:
        catalog = resolve_catalog_for_report(report)
        assert catalog is not None

        result = complete_report(report, catalog)
        if not result["ok"]:
            return None, result["message"]

        queued = process_pending_issuances()
        if queued:
            queued_names = "、".join(
                str(item["report"].get("报告编号", ""))
                for item in queued if item.get("report") is not None
            )
            if queued_names:
                return report, f"{result['message']}；同时补签排队报告 {queued_names}"

        audit = result.get("audit")
        assert audit is not None and is_passed(audit)
        return report, f"{result['message']}（审计批 {audit_batch_no(audit)}）"
