"""质量判定引擎：版本账本、采样快照复用、阈值边界与换版联动。

职责边界：

- 指纹口径（sample_key / sampling_hash / verdict_hash）在 ``versioning.py``；
- 本模块负责持久化版本账本、采样快照、判定结果和审计；
- 编排（何时复用、何时重采、如何联动清洗规则）在 ``quality.py``。

关键约束：

1. 同一规则同一时刻只允许一个「已生效」版本，换版串行化；
2. 判定结果按版本保留，旧版本结论不会被新版本覆盖，只会被标记失效；
3. 零样本、采样失败、异常中断、重试超时都是独立状态，不沿用上次通过。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Iterable

from app.services.versioning import (
    BOUNDARY_OPEN,
    content_hash,
    normalize_policy,
    sample_hash,
    sample_key,
    sampling_hash,
    to_ratio,
    verdict_hash,
)
from app.store import store

QUALITY_LEDGER = "quality_version_ledger"
QUALITY_SNAPSHOTS = "quality_sample_snapshot"
QUALITY_EVALUATIONS = "quality_evaluation"
AUDIT_MODULE = "auditlog"

# FastAPI 同步接口在线程池里并发执行；换版必须串行，
# 否则两个版本可能同时写入「已生效」。
RELEASE_LOCK = threading.RLock()

PASS = "通过"
BLOCK = "阻断"
WARN = "告警"
PENDING = "待判定"

STATE_DONE = "已完成"
STATE_BOUNDARY = "边界命中"
STATE_ZERO = "零样本"
STATE_SAMPLE_FAILED = "采样失败"
STATE_INTERRUPTED = "异常中断"
STATE_TIMEOUT = "重试超时"

FAILURE_STATES = {STATE_SAMPLE_FAILED, STATE_INTERRUPTED, STATE_TIMEOUT}
LEGAL_STATES = {STATE_DONE, STATE_BOUNDARY, STATE_ZERO, *FAILURE_STATES}

REUSE_FULL = "整版复用"
REUSE_SAMPLE = "样本复用重评"
REUSE_FULL_RERUN = "全量重采"
REUSE_AFFECTED = "受影响区间"

_TOLERANCE = 1e-9


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _rows(module: str) -> list[dict[str, Any]]:
    return store.rows(module)


def _next_id(rows: Iterable[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def batch_id(rule_no: str, version: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")[:-3]
    return f"QB-{rule_no}-{version}-{stamp}"


# ---------------------------------------------------------------------------
# 版本账本
# ---------------------------------------------------------------------------
def ledger_rows(rule_no: str) -> list[dict[str, Any]]:
    return [row for row in _rows(QUALITY_LEDGER) if row.get("规则编号") == rule_no]


def get_ledger(rule_no: str, version: str) -> dict[str, Any] | None:
    for row in ledger_rows(rule_no):
        if str(row.get("规则版本")) == str(version):
            return row
    return None


def current_effective(rule_no: str) -> dict[str, Any] | None:
    """持久化口径的当前生效版本；若存在多条，取最近发布时间。"""
    effective = [row for row in ledger_rows(rule_no) if row.get("status") == "已生效"]
    if not effective:
        return None
    return max(effective, key=lambda row: str(row.get("released_at") or ""))


def ensure_ledger(rule: dict[str, Any], *, status: str = "待评审", reviewed: bool = False) -> dict[str, Any]:
    """幂等登记：同一规则同一版本只留一条账本记录。"""
    rule_no = str(rule.get("规则编号"))
    version = str(rule.get("规则版本"))
    existing = get_ledger(rule_no, version)
    if existing is not None:
        return existing
    rows = _rows(QUALITY_LEDGER)
    row = {
        "id": _next_id(rows),
        "规则编号": rule_no,
        "规则版本": version,
        "content_hash": content_hash(rule),
        "sampling_hash": sampling_hash(rule),
        "status": status,
        "reviewed": reviewed,
        "bound_block": False,
        "released_at": None,
        "replaced_by": None,
        "reviewed_by": "数据治理评审组" if reviewed else None,
    }
    rows.append(row)
    return row


def mark_reviewed(rule: dict[str, Any], *, reviewer: str = "数据治理评审组") -> dict[str, Any]:
    ledger = ensure_ledger(rule, status="评审中", reviewed=False)
    ledger["reviewed"] = True
    ledger["reviewed_by"] = reviewer
    ledger["status"] = "待生效"
    return ledger


def release_ledger(rule: dict[str, Any], *, batch: str) -> dict[str, Any]:
    """将版本置为已生效，旧版本置为已替换。调用方必须持有 RELEASE_LOCK。"""
    rule_no = str(rule.get("规则编号"))
    version = str(rule.get("规则版本"))
    ledger = ensure_ledger(rule, status="待评审", reviewed=False)
    previous = current_effective(rule_no)
    if previous is not None and str(previous.get("规则版本")) != version:
        previous["status"] = "已替换"
        previous["replaced_by"] = version
        previous["replaced_at"] = now_iso()
    ledger["status"] = "已生效"
    ledger["reviewed"] = True
    ledger["released_at"] = now_iso()
    ledger["release_batch"] = batch
    ledger["content_hash"] = content_hash(rule)
    return ledger


# ---------------------------------------------------------------------------
# 采样快照
# ---------------------------------------------------------------------------
def snapshots_for(rule_no: str, version: str | None = None) -> list[dict[str, Any]]:
    rows = [row for row in _rows(QUALITY_SNAPSHOTS) if row.get("规则编号") == rule_no]
    if version is not None:
        rows = [row for row in rows if str(row.get("规则版本")) == str(version)]
    return sorted(rows, key=lambda row: int(row.get("id", 0)))


def find_snapshot(sample_key_value: str) -> dict[str, Any] | None:
    for row in _rows(QUALITY_SNAPSHOTS):
        if row.get("sample_key") == sample_key_value:
            return row
    return None


def record_snapshot(
    *,
    rule: dict[str, Any],
    sample_size: int,
    bad_rows: int,
    bad_rate: float,
    lower: float | None,
    upper: float | None,
    verdict: dict[str, Any],
    reused_from: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """写采样快照。同一 sample_key 且 sample_hash 相同 → 只更新复用计数。"""
    key = sample_key(rule)
    digest = sample_hash(rule)
    rows = _rows(QUALITY_SNAPSHOTS)
    existing = next((row for row in rows if row.get("sample_key") == key), None)
    if existing is not None and existing.get("sample_hash") == digest:
        existing["reuse_count"] = int(existing.get("reuse_count", 0)) + 1
        existing["last_reused_at"] = now_iso()
        if reused_from is not None:
            existing["reused_from"] = reused_from.get("sample_key")
        return existing
    row = {
        "id": _next_id(rows),
        "sample_key": key,
        "规则编号": rule.get("规则编号"),
        "规则版本": rule.get("规则版本"),
        "校验表名": rule.get("校验表名"),
        "目标字段": rule.get("目标字段"),
        "采样比例": to_ratio(rule.get("采样比例")),
        "sample_hash": digest,
        "sampling_hash": sampling_hash(rule),
        "content_hash": content_hash(rule),
        "verdict_hash": verdict_hash(rule),
        "block_level": rule.get("阻断级别"),
        "sample_size": int(sample_size),
        "bad_rows": int(bad_rows),
        "bad_rate": round(float(bad_rate), 6),
        "boundary_value": upper,
        "lower_boundary_value": lower,
        "boundary_side": verdict.get("boundary_side"),
        "boundary_hit": bool(verdict.get("boundary_hit")),
        "policy": normalize_policy(rule.get("边界策略")),
        "affected_range": rule.get("affected_range"),
        "evaluated_at": now_iso(),
        "reuse_count": 1 if reused_from else 0,
        "reused_from": reused_from.get("sample_key") if reused_from else None,
    }
    rows.append(row)
    return row


# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------
def evaluations_for(
    rule_no: str, version: str | None = None, *, active_only: bool = False
) -> list[dict[str, Any]]:
    rows = [row for row in _rows(QUALITY_EVALUATIONS) if row.get("规则编号") == rule_no]
    if version is not None:
        rows = [row for row in rows if str(row.get("规则版本")) == str(version)]
    if active_only:
        rows = [row for row in rows if row.get("active", True) and not row.get("invalidated")]
    return sorted(rows, key=lambda row: (str(row.get("evaluated_at") or ""), int(row.get("id", 0))))


def latest_evaluation(rule_no: str, version: str | None = None, active_only: bool = True) -> dict[str, Any] | None:
    rows = evaluations_for(rule_no, version, active_only=active_only)
    return rows[-1] if rows else None


def invalidate_version(rule_no: str, version: str, *, batch: str, reason: str) -> None:
    for row in _rows(QUALITY_EVALUATIONS):
        if (
            row.get("规则编号") == rule_no
            and str(row.get("规则版本")) == version
            and row.get("active", True)
            and not row.get("invalidated")
        ):
            row["active"] = False
            row["invalidated"] = True
            row["invalidated_at"] = now_iso()
            row["invalidated_batch"] = batch
            row["invalidated_reason"] = reason


def record_evaluation(
    *,
    rule: dict[str, Any],
    ledger: dict[str, Any],
    batch: str,
    verdict: dict[str, Any],
    sample_size: int,
    bad_rows: int,
    bad_rate: float,
    snapshot: dict[str, Any] | None,
    reused_from: dict[str, Any] | None,
    reuse_mode: str,
    reason: str,
    trigger: str,
    affected_range: str | None = None,
) -> dict[str, Any]:
    rows = _rows(QUALITY_EVALUATIONS)
    rule_no = str(rule.get("规则编号"))
    version = str(rule.get("规则版本"))
    row = {
        "id": _next_id(rows),
        "规则编号": rule_no,
        "规则版本": version,
        "批次": batch,
        "触发来源": trigger,
        "clean_version": rule.get("clean_version"),
        "sample_key": (snapshot or {}).get("sample_key") or sample_key(rule),
        "sample_hash": (snapshot or {}).get("sample_hash") or sample_hash(rule),
        "sample_size": int(sample_size),
        "bad_rows": int(bad_rows),
        "bad_rate": round(float(bad_rate), 6),
        "boundary_value": verdict.get("boundary_value"),
        "lower_boundary_value": verdict.get("lower_boundary_value"),
        "boundary_side": verdict.get("boundary_side"),
        "boundary_hit": bool(verdict.get("boundary_hit")),
        "boundary_policy": normalize_policy(rule.get("边界策略")),
        "state": verdict["state"],
        "conclusion": verdict["conclusion"],
        "block_status": verdict["block_status"],
        "decision": verdict.get("decision", verdict["conclusion"]),
        "reused_sample": reused_from is not None and reuse_mode in {REUSE_FULL, REUSE_SAMPLE, REUSE_AFFECTED},
        "reuse_mode": reuse_mode,
        "reused_from": reused_from.get("sample_key") if reused_from else None,
        "rerun_scope": _scope_of(reuse_mode, reused_from),
        "affected_range": affected_range,
        "reason": reason,
        "evaluated_at": now_iso(),
        "active": True,
        "invalidated": False,
    }
    rows.append(row)
    ledger["last_evaluated_at"] = row["evaluated_at"]
    ledger["last_conclusion"] = row["conclusion"]
    ledger["last_block_status"] = row["block_status"]
    ledger["last_batch"] = batch
    ledger["bound_block"] = row["block_status"] == BLOCK
    return row


def _scope_of(reuse_mode: str, reused_from: dict[str, Any] | None) -> str:
    """重跑范围口径：整版复用不重跑，样本复用只重评，受影响区间只跑区间内样本。"""
    if reuse_mode == REUSE_FULL:
        return "无需重跑"
    if reuse_mode == REUSE_SAMPLE:
        return "只重评边界"
    if reuse_mode == REUSE_AFFECTED:
        return "受影响区间"
    return REUSE_FULL_RERUN


# ---------------------------------------------------------------------------
# 阈值判定
# ---------------------------------------------------------------------------
def judge_threshold(
    *,
    bad_rate: float,
    sample_size: int,
    lower: float | None,
    upper: float | None,
    policy: str,
    failure_state: str | None = None,
) -> dict[str, Any]:
    """闭区间/开区间下的稳定结论。

    - 零样本：待判定，不复用上次通过；
    - 命中边界：显式标记 boundary_hit / boundary_side；
    - 闭区间命中边界仍在界内，开区间命中边界即越界；
    - 刚跨过阈值（含浮点误差内）按越界处理，结论不摇摆。
    """
    policy = normalize_policy(policy)
    base = {"boundary_value": upper, "lower_boundary_value": lower, "policy": policy}

    if failure_state in FAILURE_STATES:
        return {
            **base,
            "state": failure_state,
            "conclusion": PENDING,
            "block_status": PENDING,
            "decision": PENDING,
            "boundary_hit": False,
            "boundary_side": None,
            "reason": f"{failure_state}，保持待判定，不沿用上次通过",
        }
    if sample_size <= 0:
        return {
            **base,
            "state": STATE_ZERO,
            "conclusion": PENDING,
            "block_status": PENDING,
            "decision": PENDING,
            "boundary_hit": False,
            "boundary_side": None,
            "reason": "零样本不判定，不沿用上次通过结论",
        }

    rate = float(bad_rate)  # 不提前四舍五入：刚刚跨过阈值必须稳定判为越界
    if upper is not None and rate > upper + _TOLERANCE:
        return _verdict(base, BLOCK, "异常率越过上界", False, None, "上界越界")
    if lower is not None and rate < lower - _TOLERANCE:
        return _verdict(base, BLOCK, "异常率低于下界", False, None, "下界越界")

    hit_upper = upper is not None and abs(rate - upper) <= _TOLERANCE
    hit_lower = lower is not None and abs(rate - lower) <= _TOLERANCE
    if hit_upper:
        conclusion = BLOCK if policy == BOUNDARY_OPEN else PASS
        detail = "开区间命中上界视为越界" if policy == BOUNDARY_OPEN else "闭区间命中上界仍在界内"
        return _verdict(base, conclusion, detail, True, "上界", "上界命中")
    if hit_lower:
        conclusion = BLOCK if policy == BOUNDARY_OPEN else PASS
        detail = "开区间命中下界视为越界" if policy == BOUNDARY_OPEN else "闭区间命中下界仍在界内"
        return _verdict(base, conclusion, detail, True, "下界", "下界命中")
    return _verdict(base, PASS, "异常率位于阈值区间内", False, None, "区间内")


def _verdict(
    base: dict[str, Any],
    conclusion: str,
    reason: str,
    boundary_hit: bool,
    boundary_side: str | None,
    decision: str,
) -> dict[str, Any]:
    return {
        **base,
        "state": STATE_BOUNDARY if boundary_hit else STATE_DONE,
        "conclusion": conclusion,
        "block_status": conclusion,
        "decision": decision,
        "boundary_hit": boundary_hit,
        "boundary_side": boundary_side,
        "reason": reason,
    }


def apply_block_level(verdict: dict[str, Any], block_level: Any) -> dict[str, Any]:
    """越界时按阻断级别落地：阻断级保持阻断，告警级降为告警。"""
    if verdict.get("conclusion") != BLOCK:
        return verdict
    level = str(block_level or "").strip()
    if level in {"告警", "提醒"}:
        result = dict(verdict)
        result["conclusion"] = WARN
        result["block_status"] = WARN
        result["decision"] = WARN
        return result
    return verdict


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------
def write_audit(
    *,
    table: str,
    change_type: str,
    before: Any,
    after: Any,
    reason: str,
    batch: str,
    abnormal: bool = False,
    operator: str = "数据治理评审组",
) -> dict[str, Any]:
    rows = _rows(AUDIT_MODULE)
    new_id = _next_id(rows)
    row = {
        "id": new_id,
        "status": "已记录",
        "pending": False,
        "abnormal": abnormal,
        "记录编号": f"AUD-{new_id:04d}",
        "操作表名": table,
        "变更类型": change_type,
        "变更前值": before,
        "变更后值": after,
        "操作人": operator,
        "操作时间": now_iso(),
        "变更原因": reason,
        "批次": batch,
    }
    rows.append(row)
    return row
