"""版本与采样指纹的纯函数：种子数据和服务层共用同一套口径。

这个模块不依赖 store，避免 ``store -> seed -> services -> store`` 的循环导入。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

BOUNDARY_CLOSED = "闭区间"
BOUNDARY_OPEN = "开区间"
DEFAULT_BOUNDARY_POLICY = BOUNDARY_CLOSED


def stable_hash(payload: Any, size: int = 10) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:size]


def to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("%", "").replace("％", "")
    try:
        return float(text)
    except ValueError:
        return None


def to_ratio(value: Any, default: float = 0.1) -> float:
    """采样比例统一归一到 0~1 之间的小数，兼容 0.1 与 10% 两种写法。"""
    number = to_float(value)
    if number is None:
        return default
    if number > 1:
        number = number / 100
    return round(max(min(number, 1.0), 0.0), 6)


def normalize_policy(value: Any) -> str:
    text = str(value or "").strip()
    if text in {BOUNDARY_OPEN, "开", "不含边界"}:
        return BOUNDARY_OPEN
    return BOUNDARY_CLOSED


def sampling_hash(rule: dict[str, Any], clean_version: str | None = None) -> str:
    """采样计划指纹：决定「已经采过的行还能不能拿来用」。

    只看校验表、目标字段、校验表达式、采样比例和清洗版本；
    阈值和阻断级别不参与，所以只收紧阈值时不需要重新采样。
    """
    return stable_hash(
        {
            "table": rule.get("校验表名"),
            "field": rule.get("目标字段"),
            "expression": rule.get("校验表达式"),
            "ratio": to_ratio(rule.get("采样比例")),
            "clean_version": clean_version or rule.get("clean_version") or "",
        }
    )


def content_hash(rule: dict[str, Any]) -> str:
    """规则内容指纹：表达式/表/字段变化就视为内容变化，旧结论立即失效。"""
    return stable_hash(
        {
            "table": rule.get("校验表名"),
            "field": rule.get("目标字段"),
            "expression": rule.get("校验表达式"),
        }
    )


def verdict_hash(rule: dict[str, Any]) -> str:
    """判定口径指纹：阈值、边界策略和阻断级别。"""
    return stable_hash(
        {
            "lower": to_float(rule.get("阈值下界")),
            "upper": to_float(rule.get("阈值上界")),
            "policy": normalize_policy(rule.get("边界策略")),
            "block": rule.get("阻断级别"),
        }
    )


def sample_key(rule: dict[str, Any]) -> str:
    """采样结论的复用键：校验表 + 目标字段 + 规则版本 + 采样比例。"""
    return "|".join(
        [
            str(rule.get("规则编号") or ""),
            str(rule.get("规则版本") or ""),
            str(rule.get("校验表名") or ""),
            str(rule.get("目标字段") or ""),
            f"{to_ratio(rule.get('采样比例')):g}",
        ]
    )


def current_sample_key(rule: dict[str, Any]) -> str:
    """与 sample_key 相同的维度，但不含规则版本：用于跨版本找可复用样本。"""
    return "|".join(
        [
            str(rule.get("规则编号") or ""),
            str(rule.get("校验表名") or ""),
            str(rule.get("目标字段") or ""),
            f"{to_ratio(rule.get('采样比例')):g}",
        ]
    )


def sample_hash(rule: dict[str, Any], clean_version: str | None = None) -> str:
    """快照里保留的采样指纹，与 sample_key 一起用于识别重复采样。"""
    return stable_hash(
        {
            "key": sample_key(rule),
            "plan": sampling_hash(rule, clean_version),
            "content": content_hash(rule),
        }
    )
