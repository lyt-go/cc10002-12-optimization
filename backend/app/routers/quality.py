"""质量校验接口：规则维护、版本换版、采样上报与清洗联动。

列表/明细保持原有结构，新增字段用于版本、采样快照和阻断结论；
换版链路要求调用方带上 expected_version，冲突时以持久化版本为准。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult, VersionChangePayload
from app.services import quality_engine as engine
from app.services.quality import QualityService

router = APIRouter(prefix="/api/quality", tags=["质量校验"])

service = QualityService()

LIST_FIELDS = ["规则编号", "校验表名", "目标字段", "校验表达式", "阈值下界", "阈值上界", "采样比例", "阻断级别"]
STATUSES = ["草稿", "规则评审中", "已启用", "已废弃"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、规则评审中、已启用、已废弃"),
    table: str | None = Query(default=None, description="按校验表名精确过滤"),
    field: str | None = Query(default=None, description="按目标字段模糊过滤"),
    version: str | None = Query(default=None, description="按规则版本精确过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤质量校验列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, table=table, field=field, version=version, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出质量校验清单：带版本、采样快照和阻断结论。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "quality", "total": total, "items": items}


@router.get("/rules/{rule_no}")
def get_rule_detail(rule_no: str, version: str | None = None) -> dict[str, Any]:
    """读取规则的版本详情：版本列表、采样快照和判定历史。"""
    detail = service.detail(rule_no, version)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"质量规则 {rule_no} 不存在或已归档")
    return detail


@router.get("/rules/{rule_no}/versions")
def get_rule_versions(rule_no: str) -> dict[str, Any]:
    """版本下拉：每个版本的评审状态、生效状态与最新结论。"""
    options = service.version_options(rule_no)
    if not options:
        raise HTTPException(status_code=404, detail=f"质量规则 {rule_no} 没有可用版本")
    return {"rule": rule_no, "items": options}


@router.get("/table/{table}")
def get_table_quality(table: str) -> dict[str, Any]:
    """按校验表汇总质量结论：清洗规则页复用同一份结论，避免口径分叉。"""
    return {"table": table, **service.table_summary(table)}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条质量规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"质量规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条质量规则，缺字段时说明原因而不是静默丢弃。"""
    rule_no = str(payload.values.get("规则编号") or "").strip()
    version = str(payload.values.get("规则版本") or "v1").strip()
    if service.find_version(rule_no, version) is not None:
        return ActionResult(ok=False, message=f"质量规则 {rule_no} 版本 {version} 已存在，重复登记不再生效")
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="质量规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """提交评审、评审通过、启用、废弃；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/change-version", response_model=ActionResult)
def change_version(entry_id: int, payload: VersionChangePayload) -> ActionResult:
    """换版重评：同批次完成判定、版本生效与审计；expected_version 用于并发冲突。"""
    result, message = service.change_version(
        entry_id=entry_id,
        target_version=payload.target_version,
        expected_version=payload.expected_version,
        force_rerun=payload.force_rerun,
    )
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=result)


@router.post("/{entry_id}/sample", response_model=ActionResult)
def report_sample(entry_id: int, payload: EntryPayload) -> ActionResult:
    """采样上报：零样本、采样失败、异常中断、重试超时都落到明确状态。"""
    try:
        sample_size = int(payload.values.get("sample_size") or 0)
        bad_rows = int(payload.values.get("bad_rows") or 0)
    except (TypeError, ValueError):
        return ActionResult(ok=False, message="sample_size 与 bad_rows 必须是整数")
    entry, message = service.report_sample(
        entry_id,
        sample_size=sample_size,
        bad_rows=bad_rows,
        state=str(payload.values.get("state") or engine.STATE_DONE),
        reason=payload.values.get("reason"),
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
