"""治理审计领域服务：确认通过/驳回时把结论归位到对应目录并联动报告。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.catalog_report import (
    confirm_audit_pass,
    confirm_audit_reject,
    resolve_catalog_for_report,
)


class GovernAuditService(BaseModuleService):
    MODULE = "govern_audit"
    ENTITY = "审计记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["记录编号", "审计对象", "审计类型", "检查结果", "问题项", "整改方案", "审计时间", "整改状态"]
    REQUIRED_FIELDS = ["记录编号", "审计对象", "审计类型"]
    STATUS_ORDER = ["待审计", "审计中", "已通过", "整改中"]
    ACTION_RULES = {"发起审计": "审计中", "确认通过": "已通过", "指派整改": "整改中"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "确认通过":
            result = confirm_audit_pass(entry)
            return entry, result["message"]

        if action == "指派整改":
            result = confirm_audit_reject(entry)
            return entry, result["message"]

        return super().run_action(entry_id, action)

    def catalog_of(self, audit: dict[str, Any]) -> dict[str, Any] | None:
        """审计记录对应的目录；供外部核对待签材料。"""
        return resolve_catalog_for_report({
            "审计记录编号": audit.get("id"),
            "特征范围": audit.get("审计对象", ""),
        })
