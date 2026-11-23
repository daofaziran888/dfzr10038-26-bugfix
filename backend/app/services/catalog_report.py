"""特征目录发布 → 治理审计 → 特征报告的关联编排。

这条链路里有三份独立的数据，不能互相顶替：

* 目录（``feat_catalog``）只发起审计，不自行判定审计结论；
* 审计批（``govern_audit``）一次只服务一个目录，待审池里的记录被占用后
  不能再被别的目录领用，已驳回的结论更不能拿来签发；
* 成稿名额（``report_task`` 的草稿）是并发资源，谁先取得审计通过结论谁
  先领取；没有名额时只记录“待名额”，不伪造通过报告。

报告的生成必须真正读取对应目录的审计批：报告上写入审计批号、目录索引
编号和审计结论。历史报告如果特征范围能对应上目录、且平台里存在可核实的
通过审计批，则沿用原报告并补齐关联，不额外占用成稿名额。
"""
from __future__ import annotations

import datetime
import threading
from typing import Any

from app.store import store

AUDIT_MODULE = "govern_audit"
CATALOG_MODULE = "feat_catalog"
REPORT_MODULE = "report_task"

PASS_WORDS = ("通过", "合格", "pass")
REJECT_WORDS = ("不通过", "未通过", "驳回", "失败", "reject")
PENDING_AUDIT_STATUS = ("待审计", "审计中")

# FastAPI 的同步接口在线程池里执行；领取待审记录和成稿名额都是多步写入，
# 用可重入锁把这些步骤串起来，避免两个发布请求抢到同一份结论/名额。
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.datetime.now().isoformat()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _next_id(module: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(module)), default=0) + 1


def _next_code(module: str, field: str, prefix: str) -> str:
    largest = 0
    for row in store.rows(module):
        value = _text(row.get(field))
        if value.startswith(prefix) and value[len(prefix):].isdigit():
            largest = max(largest, int(value[len(prefix):]))
    return f"{prefix}{largest + 1:04d}"


def _catalog_index(catalog: dict[str, Any]) -> str:
    return _text(catalog.get("索引编号"))


def _catalog_name(catalog: dict[str, Any]) -> str:
    return _text(catalog.get("目录名称"))


def audit_batch_no(audit: dict[str, Any] | None) -> str:
    """审计批对外展示的编号；历史记录没有审计批号时退回记录编号。"""
    if not audit:
        return ""
    return _text(audit.get("审计批号")) or _text(audit.get("记录编号"))


def is_passed(audit: dict[str, Any] | None) -> bool:
    """只有真实存在的“通过”结论才算通过，缺字段一律按未通过处理。"""
    if not audit:
        return False
    result = _text(audit.get("检查结果"))
    conclusion = _text(audit.get("审计结论"))
    probe = f"{result}{conclusion}"
    if any(word in probe for word in REJECT_WORDS):
        return False
    if _text(audit.get("status")) == "已通过":
        # 状态已通过但检查结果写着“不通过”时以结果为准（上面已拦截）。
        return True
    if any(word in probe for word in PASS_WORDS):
        return True
    return False


def is_rejected(audit: dict[str, Any] | None) -> bool:
    if not audit:
        return False
    probe = f"{_text(audit.get('检查结果'))}{_text(audit.get('审计结论'))}"
    if any(word in probe for word in REJECT_WORDS):
        return True
    return _text(audit.get("status")) == "整改中"


def _identity_matches(catalog: dict[str, Any], audit: dict[str, Any]) -> bool:
    """审计批是否属于该目录：以索引编号为主，名称只用于历史数据兜底。"""
    index_no = _catalog_index(catalog)
    name = _catalog_name(catalog)
    audit_index = _text(audit.get("目录索引编号"))
    audit_name = _text(audit.get("目录名称"))
    audit_object = _text(audit.get("审计对象"))
    if audit_index:
        return bool(index_no) and audit_index == index_no
    if audit_name:
        return bool(name) and audit_name == name
    return bool(audit_object) and audit_object in {name, index_no}


def _claimed_by_other(audit: dict[str, Any], catalog: dict[str, Any]) -> bool:
    """审计批是否已经被另一个目录占用。"""
    audit_id = int(audit.get("id", 0))
    for other in store.rows(CATALOG_MODULE):
        if other is catalog:
            continue
        if int(other.get("审计记录编号") or 0) == audit_id:
            return True
    return False


def _find_bound_audit(catalog: dict[str, Any]) -> dict[str, Any] | None:
    """找本目录已经绑定过的审计批；不会拿别的目录的批。"""
    audit_id = catalog.get("审计记录编号")
    if audit_id:
        row = store.find(AUDIT_MODULE, int(audit_id))
        if row is not None and _identity_matches(catalog, row):
            return row

    batch = _text(catalog.get("审计批号"))
    if batch:
        for row in store.rows(AUDIT_MODULE):
            if batch in {audit_batch_no(row), _text(row.get("记录编号"))}:
                if _identity_matches(catalog, row):
                    return row

    for row in store.rows(AUDIT_MODULE):
        if _identity_matches(catalog, row) and not _claimed_by_other(row, catalog):
            return row
    return None


def _find_historical_passed_audit(catalog: dict[str, Any]) -> dict[str, Any] | None:
    """历史兼容：目录已有可核实的通过审计批（按名称/索引能对上）。"""
    for row in store.rows(AUDIT_MODULE):
        if not is_passed(row):
            continue
        if not _identity_matches(catalog, row):
            continue
        if _claimed_by_other(row, catalog):
            continue
        return row
    return None


def _audit_object_belongs_to_known_catalog(
    audit: dict[str, Any], catalog: dict[str, Any]
) -> bool:
    """待审池记录若已写着另一个目录的名字，不能抢过来。"""
    audit_object = _text(audit.get("审计对象"))
    if not audit_object:
        return False
    for other in store.rows(CATALOG_MODULE):
        if other is catalog:
            continue
        if audit_object in {_catalog_name(other), _catalog_index(other)}:
            return True
    return False


def _find_pool_audit(catalog: dict[str, Any]) -> dict[str, Any] | None:
    """从待审池领一条尚未被占用的记录；优先“待审计”。"""
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for row in store.rows(AUDIT_MODULE):
        if _text(row.get("status")) not in PENDING_AUDIT_STATUS:
            continue
        # 已经绑定目录的记录不在池子里。
        if _text(row.get("目录索引编号")) or _text(row.get("目录名称")):
            continue
        if _claimed_by_other(row, catalog):
            continue
        if _audit_object_belongs_to_known_catalog(row, catalog):
            continue
        # 有“通过/驳回”结论的批不是普通待审材料，不能拿来顶替。
        if is_passed(row) or is_rejected(row):
            continue
        priority = 0 if _text(row.get("status")) == "待审计" else 1
        candidates.append((priority, int(row.get("id", 0)), row))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def _bind_audit(catalog: dict[str, Any], audit: dict[str, Any]) -> None:
    """把一份审计批独占绑定到目录；只在首次绑定时补编号。"""
    index_no = _catalog_index(catalog)
    name = _catalog_name(catalog)
    audit["目录索引编号"] = index_no
    audit["目录名称"] = name
    audit["审计对象"] = name
    if not _text(audit.get("审计批号")):
        audit["审计批号"] = _text(audit.get("记录编号")) or f"AUD-{int(audit.get('id', 0)):04d}"
    if _text(audit.get("审计类型")) in {"", "治理审计样例1", "治理审计样例2", "治理审计样例3"}:
        audit["审计类型"] = "目录发布审计"
    audit.setdefault("审计类型", "目录发布审计")
    audit["绑定时间"] = _now()
    if not is_passed(audit) and not is_rejected(audit):
        audit["status"] = "审计中"
        audit["pending"] = True
        audit["abnormal"] = False
        # 待审材料的检查结果只保留真实结论，清掉样例占位值。
        if "样例" in _text(audit.get("检查结果")):
            audit["检查结果"] = ""


def _sync_catalog_audit(catalog: dict[str, Any], audit: dict[str, Any] | None) -> None:
    if audit is None:
        return
    catalog["审计记录编号"] = int(audit.get("id", 0))
    catalog["审计批号"] = audit_batch_no(audit)
    catalog["审计状态"] = _text(audit.get("status"))
    if is_rejected(audit):
        catalog["审计结论"] = "驳回"
    elif is_passed(audit):
        catalog["审计结论"] = "通过"
        catalog.setdefault("审计通过时间", _text(audit.get("审计时间")) or _now())
    else:
        catalog["审计结论"] = "待定"


def _create_audit(catalog: dict[str, Any]) -> dict[str, Any]:
    """待审池没有空位时为目录建立一份真实审计材料，绝不伪造通过结论。"""
    audit_id = _next_id(AUDIT_MODULE)
    code = _next_code(AUDIT_MODULE, "记录编号", "GOVE-")
    row: dict[str, Any] = {
        "id": audit_id,
        "status": "待审计",
        "pending": True,
        "abnormal": False,
        "记录编号": code,
        "审计对象": _catalog_name(catalog),
        "审计类型": "目录发布审计",
        "检查结果": "",
        "问题项": "",
        "整改方案": "",
        "审计时间": "",
        "整改状态": "待审计",
        "目录索引编号": _catalog_index(catalog),
        "目录名称": _catalog_name(catalog),
        "审计批号": code,
        "创建时间": _now(),
    }
    store.rows(AUDIT_MODULE).append(row)
    return row


def ensure_catalog_audit(
    catalog: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """取得目录对应的审计批；返回 (审计批, 是否新建)。"""
    with _LOCK:
        audit = _find_bound_audit(catalog)
        if audit is not None:
            _sync_catalog_audit(catalog, audit)
            return audit, False

        historical = _find_historical_passed_audit(catalog)
        if historical is not None:
            _bind_audit(catalog, historical)
            _sync_catalog_audit(catalog, historical)
            return historical, False

        audit = _find_pool_audit(catalog)
        created = False
        if audit is None:
            audit = _create_audit(catalog)
            created = True
        _bind_audit(catalog, audit)
        _sync_catalog_audit(catalog, audit)
        return audit, created


def bind_catalog_for_audit(audit: dict[str, Any]) -> dict[str, Any] | None:
    """审计人员确认结论时，把审计批归位到对应目录（支持历史按名称记录）。"""
    index_no = _text(audit.get("目录索引编号"))
    name = _text(audit.get("目录名称"))
    audit_object = _text(audit.get("审计对象"))

    catalog: dict[str, Any] | None = None
    for row in store.rows(CATALOG_MODULE):
        if index_no and _catalog_index(row) == index_no:
            catalog = row
            break
    if catalog is None and name:
        for row in store.rows(CATALOG_MODULE):
            if _catalog_name(row) == name:
                catalog = row
                break
    if catalog is None and audit_object:
        for row in store.rows(CATALOG_MODULE):
            if audit_object in {_catalog_name(row), _catalog_index(row)}:
                catalog = row
                break
    if catalog is None:
        return None

    # 审计批已被另一个目录占用时，不能因为名称相似就挪走。
    bound_id = audit.get("id")
    for other in store.rows(CATALOG_MODULE):
        if other is catalog:
            continue
        if int(other.get("审计记录编号") or 0) == int(bound_id or 0):
            return None

    _bind_audit(catalog, audit)
    _sync_catalog_audit(catalog, audit)
    catalog.setdefault("审计记录编号", int(audit.get("id", 0)))
    return catalog


# ---------------------------------------------------------------------------
# 报告签发
# ---------------------------------------------------------------------------


def _report_bound_to_other_catalog(
    report: dict[str, Any], catalog: dict[str, Any]
) -> bool:
    index_no = _text(report.get("目录索引编号"))
    return bool(index_no) and index_no != _catalog_index(catalog)


def _report_matches_catalog(report: dict[str, Any], catalog: dict[str, Any]) -> bool:
    if _report_bound_to_other_catalog(report, catalog):
        return False
    index_no = _text(report.get("目录索引编号"))
    if index_no:
        return index_no == _catalog_index(catalog)
    return _text(report.get("特征范围")) == _catalog_name(catalog)


def _report_references_audit(report: dict[str, Any], audit: dict[str, Any]) -> bool:
    audit_id = str(audit.get("id", ""))
    if _text(report.get("审计记录编号")):
        return _text(report.get("审计记录编号")) == audit_id
    batch = _text(report.get("审计批号"))
    if batch:
        return batch in {audit_batch_no(audit), _text(audit.get("记录编号"))}
    return False


def _fill_report_link(
    report: dict[str, Any], catalog: dict[str, Any], audit: dict[str, Any]
) -> None:
    """把报告真正指向本目录的审计批，这是“审计依据”的落点。"""
    batch = audit_batch_no(audit)
    report["目录索引编号"] = _catalog_index(catalog)
    report["审计记录编号"] = int(audit.get("id", 0))
    report["审计批号"] = batch
    report["审计依据"] = batch
    report["审计对象"] = _catalog_name(catalog)
    report["审计结论"] = "通过"


def _is_catalog_pass_report(report: dict[str, Any]) -> bool:
    """只有目录通过报告能被复用；质量异常等其它报告不能顶替。"""
    if report.get("status") != "已生成":
        return False
    if _text(report.get("报告类型")) == "质量异常报告":
        return False
    return True


def _find_verified_report(
    catalog: dict[str, Any], audit: dict[str, Any]
) -> dict[str, Any] | None:
    """历史兼容：已生成且来源可核实的报告不再新占成稿名额。"""
    report_id = catalog.get("报告编号")
    if report_id:
        candidate = None
        for row in store.rows(REPORT_MODULE):
            if _text(row.get("报告编号")) == _text(report_id):
                candidate = row
                break
        if (
            candidate is not None
            and _is_catalog_pass_report(candidate)
            and _report_matches_catalog(candidate, catalog)
            and (
                _report_references_audit(candidate, audit)
                or not _text(candidate.get("审计记录编号"))
            )
        ):
            return candidate

    for row in store.rows(REPORT_MODULE):
        if not _is_catalog_pass_report(row):
            continue
        if not _report_matches_catalog(row, catalog):
            continue
        if _report_references_audit(row, audit):
            return row
        # 老报告没有关联字段，但特征范围能对上且当前存在通过审计批，
        # 视为来源可核实，补齐关联即可。
        if not _text(row.get("审计记录编号")) and not _text(row.get("审计批号")):
            return row
    return None


def _has_free_draft(catalog: dict[str, Any] | None = None) -> bool:
    return _find_free_draft(catalog) is not None


def _draft_reserved_for_other(
    draft: dict[str, Any], catalog: dict[str, Any] | None
) -> bool:
    """草稿是否被明确留给另一个目录（只认系统写入的占用字段）。

    特征范围是报告的目标范围，不是名额归属：成稿名额是共享池，谁先取得
    通过结论谁先领用，因此不能用特征范围把名额锁给某个目录。
    """
    index_no = _catalog_index(catalog) if catalog is not None else ""
    draft_index = _text(draft.get("目录索引编号"))
    if draft_index and draft_index != index_no:
        return True
    occupancy = _text(draft.get("签发占用目录"))
    if occupancy and occupancy != index_no:
        return True
    return False


def _find_free_draft(catalog: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """可领用的成稿名额：显式写给本目录的优先，其次是无主草稿。"""
    drafts = [
        row for row in store.rows(REPORT_MODULE)
        if row.get("status") == "草稿"
        and not _text(row.get("审计记录编号"))
        and not _text(row.get("审计批号"))
        and not _draft_reserved_for_other(row, catalog)
    ]
    if catalog is not None:
        index_no = _catalog_index(catalog)
        for row in drafts:
            if _text(row.get("目录索引编号")) == index_no:
                return row
    for row in drafts:
        # 明确标成其他目录占用的草稿不能挪用。
        if _text(row.get("目录索引编号")):
            continue
        occupancy = _text(row.get("签发占用目录"))
        if occupancy and (catalog is None or occupancy != _catalog_index(catalog)):
            continue
        return row
    return None


def resolve_catalog_for_report(report: dict[str, Any]) -> dict[str, Any] | None:
    """报告属于哪个目录：优先目录索引编号，其次特征范围，最后审计关联。"""
    index_no = _text(report.get("目录索引编号"))
    if index_no:
        for row in store.rows(CATALOG_MODULE):
            if _catalog_index(row) == index_no:
                return row
    scope = _text(report.get("特征范围"))
    if scope:
        for row in store.rows(CATALOG_MODULE):
            if scope in {_catalog_name(row), _catalog_index(row)}:
                return row
    audit_id = report.get("审计记录编号")
    if audit_id:
        audit = store.find(AUDIT_MODULE, int(audit_id))
        if audit is not None:
            for row in store.rows(CATALOG_MODULE):
                if int(row.get("审计记录编号") or 0) == int(audit.get("id", 0)):
                    return row
    return None


def complete_report(
    report: dict[str, Any], catalog: dict[str, Any]
) -> dict[str, Any]:
    """用目录的真实审计结论完成一份报告；没有通过结论则拒绝。"""
    with _LOCK:
        if _report_bound_to_other_catalog(report, catalog):
            return {
                "ok": False, "state": "待核", "reason": "报告属于其他目录",
                "audit": None, "report": report,
                "message": "该报告已关联其他目录，不能挪用为当前目录签发",
            }

        audit = _find_bound_audit(catalog)
        if audit is None or not is_passed(audit):
            if audit is not None and is_rejected(audit):
                _set_state(catalog, "已驳回", "审计未通过")
                reason = "审计未通过"
            else:
                _set_state(catalog, "待核", "待审计结论")
                reason = "待审计结论"
            return {
                "ok": False, "state": "已驳回" if reason == "审计未通过" else "待核",
                "reason": reason, "audit": audit, "report": report,
                "message": f"目录尚未取得真实的通过审计结论（{reason}），不能完成报告生成",
            }

        # 一份草稿只能被一个目录领取；已经被其他目录签发的不再覆盖。
        if _text(report.get("审计记录编号")) and not _report_references_audit(report, audit):
            return {
                "ok": False, "state": "待核", "reason": "成稿名额已被占用",
                "audit": audit, "report": report,
                "message": "该成稿名额已被其他审计批占用，不能顶替签发",
            }

        report["status"] = "已生成"
        report["pending"] = False
        report["abnormal"] = False
        if not _text(report.get("报告类型")) or report.get("报告类型", "").endswith("样例1"):
            report["报告类型"] = "特征报告"
        report["特征范围"] = _catalog_name(catalog)
        report["最近生成"] = _now()
        report["签发时间"] = _now()
        report["签发状态"] = "已签发"
        report["待办原因"] = ""
        report["triggered_by"] = f"feat_catalog/{_catalog_index(catalog)}"
        _fill_report_link(report, catalog, audit)

        catalog["报告编号"] = _text(report.get("报告编号"))
        catalog["报告状态"] = "已生成"
        catalog.pop("签发排队时间", None)
        _set_state(catalog, "已签发", "")

        audit["报告编号"] = _text(report.get("报告编号"))
        audit["报告状态"] = "已生成"
        audit["签发时间"] = _now()

        return {
            "ok": True, "issued": True, "state": "已签发", "reason": "",
            "audit": audit, "report": report,
            "message": f"审计批 {audit_batch_no(audit)} 已通过，报告 "
                       f"{_text(report.get('报告编号'))} 已签发",
        }


def _set_state(catalog: dict[str, Any], state: str, reason: str) -> None:
    catalog["签发状态"] = state
    catalog["待办原因"] = reason


def _issue_locked(catalog: dict[str, Any]) -> dict[str, Any]:
    audit = _find_bound_audit(catalog)
    if audit is None:
        _set_state(catalog, "待核", "待审计结论")
        return {
            "ok": False, "issued": False, "state": "待核",
            "reason": "待审计结论", "audit": None, "report": None,
            "message": "目录尚未取得对应的审计批，报告待核，不能签发通过报告",
        }

    _sync_catalog_audit(catalog, audit)

    if is_rejected(audit):
        _set_state(catalog, "已驳回", "审计未通过")
        return {
            "ok": False, "issued": False, "state": "已驳回",
            "reason": "审计未通过", "audit": audit, "report": None,
            "message": f"审计批 {audit_batch_no(audit)} 已驳回，不能签发通过报告",
        }

    if not is_passed(audit):
        _set_state(catalog, "待核", "待审计结论")
        return {
            "ok": False, "issued": False, "state": "待核",
            "reason": "待审计结论", "audit": audit, "report": None,
            "message": f"审计批 {audit_batch_no(audit)} 尚无正式通过结论，报告待核",
        }

    # 已经有可核实的历史报告时，补关联即可，不再消耗新的成稿名额。
    existing = _find_verified_report(catalog, audit)
    if existing is not None:
        if not _text(existing.get("审计记录编号")):
            _fill_report_link(existing, catalog, audit)
        existing["签发状态"] = "已签发"
        existing["待办原因"] = ""
        catalog["报告编号"] = _text(existing.get("报告编号"))
        catalog["报告状态"] = "已生成"
        _set_state(catalog, "已签发", "")
        audit["报告编号"] = _text(existing.get("报告编号"))
        audit["报告状态"] = "已生成"
        return {
            "ok": True, "issued": False, "already": True, "state": "已签发",
            "reason": "", "audit": audit, "report": existing,
            "message": f"审计批 {audit_batch_no(audit)} 已有可核实的报告 "
                       f"{_text(existing.get('报告编号'))}，已补齐审计依据",
        }

    draft = _find_free_draft(catalog)
    if draft is None:
        _set_state(catalog, "待名额", "成稿名额已用尽")
        catalog.setdefault("签发排队时间", _now())
        return {
            "ok": False, "issued": False, "state": "待名额",
            "reason": "成稿名额已用尽", "audit": audit, "report": None,
            "message": f"审计批 {audit_batch_no(audit)} 已通过，但成稿名额已用尽，报告待名额",
        }

    # 领取成稿名额并签发：报告必须引用本目录的审计批。
    draft["status"] = "已生成"
    draft["pending"] = False
    draft["abnormal"] = False
    draft["报告类型"] = "特征报告"
    draft["特征范围"] = _catalog_name(catalog)
    draft["最近生成"] = _now()
    draft["签发时间"] = _now()
    draft["签发状态"] = "已签发"
    draft["待办原因"] = ""
    draft["triggered_by"] = f"feat_catalog/{_catalog_index(catalog)}"
    _fill_report_link(draft, catalog, audit)

    catalog["报告编号"] = _text(draft.get("报告编号"))
    catalog["报告状态"] = "已生成"
    catalog.pop("签发排队时间", None)
    _set_state(catalog, "已签发", "")

    audit["报告编号"] = _text(draft.get("报告编号"))
    audit["报告状态"] = "已生成"
    audit["签发时间"] = _now()

    return {
        "ok": True, "issued": True, "already": False, "state": "已签发",
        "reason": "", "audit": audit, "report": draft,
        "message": f"审计批 {audit_batch_no(audit)} 已通过，报告 "
                   f"{_text(draft.get('报告编号'))} 已签发",
    }


def issue_report_for_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    """按目录当前真实审计结论签发报告；不会挪用其他目录的结论。"""
    with _LOCK:
        return _issue_locked(catalog)


def publish_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    """发布目录：先落状态，再发起/复用本目录审计，最后尝试签发报告。"""
    with _LOCK:
        target = "已发布"
        catalog["status"] = target
        catalog["pending"] = True
        catalog["abnormal"] = False

        audit, created = ensure_catalog_audit(catalog)
        result = _issue_locked(catalog)
        result["created_audit"] = created
        result["published"] = True

        if audit is not None:
            _sync_catalog_audit(catalog, audit)

        if created:
            prefix = "目录已发布，待审池无空位，已建立本目录审计材料"
        else:
            prefix = "目录已发布"
        result["message"] = f"{prefix}；{result['message']}"
        return result


def confirm_audit_pass(audit: dict[str, Any]) -> dict[str, Any]:
    """审计确认通过：写真实结论，并尝试为对应目录签发报告。"""
    with _LOCK:
        audit["status"] = "已通过"
        audit["pending"] = False
        audit["abnormal"] = False
        audit["检查结果"] = "通过"
        audit["审计结论"] = "通过"
        audit["审计时间"] = _now()
        audit["整改状态"] = "无需整改"

        catalog = bind_catalog_for_audit(audit)
        if catalog is None:
            return {
                "audit": audit, "catalog": None, "issue": None,
                "message": f"审计记录 {_text(audit.get('记录编号'))} 已确认通过",
            }

        _sync_catalog_audit(catalog, audit)
        result = _issue_locked(catalog)
        return {
            "audit": audit, "catalog": catalog, "issue": result,
            "message": f"审计记录 {_text(audit.get('记录编号'))} 已确认通过；"
                       f"{result['message']}",
        }


def confirm_audit_reject(audit: dict[str, Any]) -> dict[str, Any]:
    """审计驳回：对应目录只能看到“审计未通过”，不会签发通过报告。"""
    with _LOCK:
        audit["status"] = "整改中"
        audit["pending"] = True
        audit["abnormal"] = True
        audit["检查结果"] = "不通过"
        audit["审计结论"] = "驳回"
        audit["审计时间"] = _now()
        audit["整改状态"] = "待整改"

        catalog = bind_catalog_for_audit(audit)
        if catalog is not None:
            _sync_catalog_audit(catalog, audit)
            _set_state(catalog, "已驳回", "审计未通过")
        return {
            "audit": audit, "catalog": catalog,
            "message": f"审计记录 {_text(audit.get('记录编号'))} 已驳回",
        }


def process_pending_issuances() -> list[dict[str, Any]]:
    """草稿名额补足后，按排队先后把通过结论签发到各自目录。

    以“已发布、审计已通过、还没有可核实报告”的目录为队列，排队时刻优先
    取首次遇到名额不足的时间，缺少时退回审计通过时间。
    """
    with _LOCK:
        pending: list[tuple[str, int, dict[str, Any]]] = []
        for catalog in store.rows(CATALOG_MODULE):
            if _text(catalog.get("status")) != "已发布":
                continue
            audit = _find_bound_audit(catalog)
            if audit is None or not is_passed(audit):
                continue
            if _find_verified_report(catalog, audit) is not None:
                continue
            stamp = (
                _text(catalog.get("签发排队时间"))
                or _text(catalog.get("审计通过时间"))
                or _text(audit.get("审计时间"))
            )
            pending.append((stamp, int(catalog.get("id", 0)), catalog))
        pending.sort(key=lambda item: (item[0], item[1]))

        results = []
        for _, _, catalog in pending:
            # 每签发一份都会消耗一个名额，逐个确认后再继续。
            if not _has_free_draft(catalog):
                continue
            results.append(_issue_locked(catalog))
        return results


def describe_catalog_flow(catalog: dict[str, Any]) -> list[tuple[str, str, int, str]]:
    """读取目录当前的审计/报告关联，输出链路副作用（不改变任何数据）。"""
    audit = _find_bound_audit(catalog)
    report = None
    report_id = _text(catalog.get("报告编号"))
    for row in store.rows(REPORT_MODULE):
        if report_id and _text(row.get("报告编号")) == report_id:
            report = row
            break
    if report is None and audit is not None:
        for row in store.rows(REPORT_MODULE):
            if row.get("status") == "已生成" and _report_references_audit(row, audit):
                report = row
                break

    effects: list[tuple[str, str, int, str]] = []
    index_no = _catalog_index(catalog)
    name = _catalog_name(catalog)
    if audit is not None:
        effects.append((
            "发起审计", AUDIT_MODULE, int(audit.get("id", 0)),
            f"目录 {index_no}({name}) 绑定审计批 {audit_batch_no(audit)}",
        ))
    if report is not None and report.get("status") == "已生成":
        effects.append((
            "启动生成", REPORT_MODULE, int(report.get("id", 0)),
            f"审计批 {audit_batch_no(audit)} 通过后生成特征报告 {_text(report.get('报告编号'))}",
        ))
    else:
        reason = _text(catalog.get("待办原因")) or "待核"
        effects.append((
            _text(catalog.get("签发状态")) or "待核", REPORT_MODULE, 0,
            f"目录 {index_no} 报告{reason}，本次未签发",
        ))
    return effects


def catalog_report_status(catalog: dict[str, Any]) -> dict[str, Any]:
    """查询目录的报告签发状态及“待核/待名额”的原因。"""
    with _LOCK:
        audit = _find_bound_audit(catalog)
        if audit is not None:
            _sync_catalog_audit(catalog, audit)

        report = None
        report_id = _text(catalog.get("报告编号"))
        if report_id:
            for row in store.rows(REPORT_MODULE):
                if _text(row.get("报告编号")) == report_id:
                    report = row
                    break

        if report is None:
            for row in store.rows(REPORT_MODULE):
                if row.get("status") != "已生成":
                    continue
                if _report_matches_catalog(row, catalog) and (
                    audit is None or _report_references_audit(row, audit)
                ):
                    report = row
                    break

        if report is not None and report.get("status") == "已生成":
            state, reason = "已签发", ""
        elif audit is None:
            state, reason = "待核", "待审计结论"
        elif is_rejected(audit):
            state, reason = "已驳回", "审计未通过"
        elif not is_passed(audit):
            state, reason = "待核", "待审计结论"
        elif _has_free_draft(catalog):
            state, reason = "可签发", ""
        else:
            state, reason = "待名额", "成稿名额已用尽"

        _set_state(catalog, state, reason)
        return {
            "module": CATALOG_MODULE,
            "entry_id": int(catalog.get("id", 0)),
            "索引编号": _catalog_index(catalog),
            "目录名称": _catalog_name(catalog),
            "签发状态": state,
            "待办原因": reason,
            "审计记录编号": catalog.get("审计记录编号"),
            "审计批号": catalog.get("审计批号"),
            "审计状态": _text(audit.get("status")) if audit else "",
            "审计结论": catalog.get("审计结论", ""),
            "报告编号": _text(report.get("报告编号")) if report else "",
            "报告状态": _text(report.get("status")) if report else "",
            "审计依据": _text(report.get("审计依据")) if report else "",
        }
