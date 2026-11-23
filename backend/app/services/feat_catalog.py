"""特征目录领域服务：发布目录时走审计批—成稿名额的真实链路。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.catalog_report import (
    catalog_report_status,
    issue_report_for_catalog,
    publish_catalog,
)

ISSUE_ACTIONS = {"签发报告", "领取签发", "签发通过报告", "领取成稿名额并签发", "生成报告"}


def _is_issue_action(action: str) -> bool:
    return action in ISSUE_ACTIONS or "签发" in action or "领用" in action or "领取" in action


class FeatCatalogService(BaseModuleService):
    MODULE = "feat_catalog"
    ENTITY = "目录索引"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["索引编号", "目录名称", "父目录", "目录层级", "关联特征", "目录描述", "排序权重", "创建时间"]
    REQUIRED_FIELDS = ["索引编号", "目录名称", "父目录"]
    STATUS_ORDER = ["草稿", "已发布", "重构中", "已归档"]
    ACTION_RULES = {"发布目录": "已发布", "重构层级": "重构中", "归档目录": "已归档"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "发布目录":
            result = publish_catalog(entry)
            return entry, result["message"]

        if _is_issue_action(action):
            if entry.get("status") != "已发布":
                return None, f"{self.ENTITY} {entry_id} 尚未发布，不能签发报告"
            result = issue_report_for_catalog(entry)
            return entry, result["message"]

        return super().run_action(entry_id, action)

    def report_status(self, entry_id: int) -> dict[str, Any] | None:
        entry = self.get_entry(entry_id)
        if entry is None:
            return None
        return catalog_report_status(entry)
