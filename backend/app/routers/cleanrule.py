"""清洗规则接口：维护清洗规则，覆盖提交规则评审、启用规则、废弃规则等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, CleanruleChangePayload, EntryPayload, PageResult
from app.services.cleanrule import CleanruleService

router = APIRouter(prefix="/api/cleanrule", tags=["清洗规则"])

service = CleanruleService()

LIST_FIELDS = ["规则编号", "输入表名", "空值策略", "格式表达式", "异常阈值", "输出字段", "规则版本", "生效日期"]
STATUSES = ["草稿", "规则评审中", "已启用", "已废弃"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、规则评审中、已启用、已废弃"),
    table: str | None = Query(default=None, description="按输入表名精确过滤"),
    version: str | None = Query(default=None, description="按规则版本精确过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤清洗规则列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, table=table, version=version, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条清洗规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"清洗规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条清洗规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="清洗规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条清洗规则执行提交规则评审、启用规则、废弃规则；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/rules/{rule_no}")
def get_rule_detail(rule_no: str, version: str | None = None) -> dict[str, Any]:
    """读取清洗规则的版本详情：版本列表、输出字段与复用后的质量结论。"""
    detail = service.detail(rule_no, version)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"清洗规则 {rule_no} 不存在或已归档")
    return detail


@router.get("/rules/{rule_no}/versions")
def get_rule_versions(rule_no: str) -> dict[str, Any]:
    """版本下拉：评审状态、生效状态、输出字段与受影响区间。"""
    options = service.version_options(rule_no)
    if not options:
        raise HTTPException(status_code=404, detail=f"清洗规则 {rule_no} 没有可用版本")
    return {"rule": rule_no, "items": options}


@router.post("/{entry_id}/change-version", response_model=ActionResult)
def change_version(entry_id: int, payload: CleanruleChangePayload) -> ActionResult:
    """清洗规则换版：输出字段变化时标记受影响区间并触发质量联动重评。"""
    result, message = service.change_version(
        entry_id=entry_id,
        target_version=payload.target_version,
        expected_version=payload.expected_version,
        affected_from=payload.affected_from,
        affected_to=payload.affected_to,
    )
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=result)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出清洗规则清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "cleanrule", "total": total, "items": items}
