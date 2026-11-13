"""质量换版判定链路的回归测试。

覆盖：采样复用、阈值收紧后阻断、边界闭/开区间、零样本、失败态、
重复采样只生效一次、历史结论按版本保留、并发换版冲突、清洗换版联动。
"""
from __future__ import annotations

import concurrent.futures

import pytest

from app.services import quality_engine as engine
from app.services.cleanrule import CleanruleService
from app.services.quality import QualityService


@pytest.fixture()
def quality() -> QualityService:
    return QualityService()


@pytest.fixture()
def cleanrule() -> CleanruleService:
    return CleanruleService()


def _approve(service: QualityService, rule_no: str, version: str) -> dict:
    rule = service.find_version(rule_no, version)
    assert rule is not None
    _, message = service.run_action(rule["id"], "提交规则评审")
    assert "已提交评审" in message
    _, message = service.run_action(rule["id"], "评审通过")
    assert "已评审通过" in message
    return rule


# ---------------------------------------------------------------------------
# 采样复用与换版阻断
# ---------------------------------------------------------------------------
def test_tighten_upper_bound_reuses_sample_and_blocks(quality: QualityService) -> None:
    """v2 采样 10% 异常率 2.4% 通过；v3 只把上界 3% 收紧到 2%，复用样本改判阻断。"""
    rule = _approve(quality, "QUAL_PAY_AMOUNT", "v3")
    previous = engine.latest_evaluation("QUAL_PAY_AMOUNT", "v2")
    assert previous is not None and previous["conclusion"] == engine.PASS

    result, message = quality.change_version(
        entry_id=rule["id"], target_version="v3", expected_version="v2"
    )
    assert result is not None, message
    assert result["reuse_mode"] == engine.REUSE_SAMPLE
    assert result["reused_sample"] is True
    assert result["sample_size"] == previous["sample_size"] == 1000
    assert result["bad_rows"] == previous["bad_rows"] == 24
    assert result["bad_rate"] == pytest.approx(2.4)
    assert result["conclusion"] == engine.BLOCK
    assert result["block_status"] == engine.BLOCK

    # 历史 v2 结论按当时版本保留，且仍然是活跃结论。
    old = engine.latest_evaluation("QUAL_PAY_AMOUNT", "v2", active_only=True)
    assert old is not None and old["conclusion"] == engine.PASS

    # 采样快照只多出一条 v3，且标记为复用来源。
    v3_snapshots = engine.snapshots_for("QUAL_PAY_AMOUNT", "v3")
    assert len(v3_snapshots) == 1
    assert v3_snapshots[0]["reused_from"] == previous["sample_key"]


def test_repeat_evaluation_same_version_only_once(quality: QualityService) -> None:
    """同一规则版本重复判定只保留一条活跃结论，旧结论被标记失效。"""
    rule = _approve(quality, "QUAL_PAY_AMOUNT", "v3")
    quality.change_version(entry_id=rule["id"], target_version="v3", expected_version="v2")
    quality.change_version(entry_id=rule["id"], target_version="v3", expected_version="v3")

    active = engine.evaluations_for("QUAL_PAY_AMOUNT", "v3", active_only=True)
    all_rows = engine.evaluations_for("QUAL_PAY_AMOUNT", "v3", active_only=False)
    assert len(active) == 1
    assert len(all_rows) == 2
    assert all_rows[0]["invalidated"] is True

    snapshots = engine.snapshots_for("QUAL_PAY_AMOUNT", "v3")
    assert len(snapshots) == 1
    assert snapshots[0]["reuse_count"] >= 1


def test_content_change_invalidates_old_sample(quality: QualityService) -> None:
    """规则内容变化时旧样本立即失效，不允许把旧版本通过挂到新版本上。"""
    rule = _approve(quality, "QUAL_PAY_AMOUNT", "v3")
    rule["校验表达式"] = "pay_amount IS NOT NULL AND pay_amount > 100 AND pay_amount >= 0"
    result, message = quality.change_version(
        entry_id=rule["id"], target_version="v3", expected_version="v2"
    )
    assert result is not None, message
    assert result["reuse_mode"] == engine.REUSE_FULL_RERUN
    assert result["reused_sample"] is False
    assert result["state"] == engine.STATE_ZERO
    assert result["conclusion"] == engine.PENDING


def test_block_level_change_invalidates(quality: QualityService) -> None:
    """阻断级别变化属于判定口径变化：需要重采，不能沿用旧样本结论。"""
    rule = _approve(quality, "QUAL_PAY_AMOUNT", "v3")
    rule["阻断级别"] = "告警"
    result, message = quality.change_version(
        entry_id=rule["id"], target_version="v3", expected_version="v2"
    )
    assert result is not None, message
    # 阻断级别变化后判定口径变化，会重新采样；样本为空时进入待判定。
    assert result["reuse_mode"] == engine.REUSE_FULL_RERUN
    assert result["conclusion"] in {engine.PENDING, engine.WARN}


# ---------------------------------------------------------------------------
# 阈值边界与异常状态
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("policy", "expected"),
    [("闭区间", engine.PASS), ("开区间", engine.BLOCK)],
)
def test_boundary_hit_respects_policy(policy: str, expected: str) -> None:
    verdict = engine.judge_threshold(
        bad_rate=2.0, sample_size=1000, lower=0.0, upper=2.0, policy=policy
    )
    assert verdict["state"] == engine.STATE_BOUNDARY
    assert verdict["boundary_hit"] is True
    assert verdict["boundary_side"] == "上界"
    assert verdict["conclusion"] == expected


def test_zero_sample_is_pending_and_never_reuses_pass() -> None:
    verdict = engine.judge_threshold(
        bad_rate=0.0, sample_size=0, lower=0.0, upper=2.0, policy="闭区间"
    )
    assert verdict["state"] == engine.STATE_ZERO
    assert verdict["conclusion"] == engine.PENDING
    assert verdict["block_status"] == engine.PENDING


def test_just_crossing_threshold_blocks_stably() -> None:
    """异常率刚好跨过阈值（含浮点误差外）必须稳定判为阻断。"""
    assert engine.judge_threshold(
        bad_rate=2.0000001, sample_size=1000, lower=0.0, upper=2.0, policy="闭区间"
    )["conclusion"] == engine.BLOCK
    assert engine.judge_threshold(
        bad_rate=2.0, sample_size=1000, lower=0.0, upper=2.0, policy="闭区间"
    )["conclusion"] == engine.PASS


@pytest.mark.parametrize(
    "state",
    [engine.STATE_SAMPLE_FAILED, engine.STATE_INTERRUPTED, engine.STATE_TIMEOUT],
)
def test_failure_states_are_pending(state: str) -> None:
    verdict = engine.judge_threshold(
        bad_rate=0.0,
        sample_size=0,
        lower=0.0,
        upper=2.0,
        policy="闭区间",
        failure_state=state,
    )
    assert verdict["state"] == state
    assert verdict["conclusion"] == engine.PENDING


def test_report_sample_failure_cannot_reuse_pass(quality: QualityService) -> None:
    """采样失败上报后，即使历史通过也不能直接挂到本次结论。"""
    rule = _approve(quality, "QUAL_PAY_AMOUNT", "v3")
    _, message = quality.report_sample(rule["id"], sample_size=1000, bad_rows=24, state=engine.STATE_SAMPLE_FAILED)
    assert "不能沿用上次通过" in message
    result, message = quality.change_version(
        entry_id=rule["id"], target_version="v3", expected_version="v2"
    )
    assert result is not None, message
    assert result["state"] == engine.STATE_SAMPLE_FAILED
    assert result["conclusion"] == engine.PENDING


def test_zero_sample_rule_never_inherits_pass(quality: QualityService) -> None:
    """零样本规则判定为待判定，不沿用上次通过。"""
    rule = quality.find_version("QUAL_ZERO_SAMPLE", "v1")
    assert rule is not None
    result, message = quality.change_version(
        entry_id=rule["id"], target_version="v1", expected_version="v1"
    )
    assert result is not None, message
    assert result["state"] == engine.STATE_ZERO
    assert result["conclusion"] == engine.PENDING


# ---------------------------------------------------------------------------
# 并发换版
# ---------------------------------------------------------------------------
def test_concurrent_version_change_only_one_wins(quality: QualityService) -> None:
    rule = _approve(quality, "QUAL_PAY_AMOUNT", "v3")

    def attempt(expected: str) -> tuple[dict | None, str]:
        return QualityService().change_version(
            entry_id=rule["id"], target_version="v3", expected_version=expected
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(attempt, "v2") for _ in range(3)]
        futures.append(pool.submit(attempt, "v1"))
        outcomes = [future.result() for future in futures]

    successes = [result for result, _ in outcomes if result is not None]
    assert len(successes) == 1
    effective = [row for row in engine.ledger_rows("QUAL_PAY_AMOUNT") if row["status"] == "已生效"]
    assert len(effective) == 1 and effective[0]["规则版本"] == "v3"
    assert len(engine.evaluations_for("QUAL_PAY_AMOUNT", "v3", active_only=True)) == 1


def test_change_version_requires_review(quality: QualityService) -> None:
    """未评审版本不能生效，冲突时以持久化版本为准。"""
    rule = quality.find_version("QUAL_PAY_AMOUNT", "v3")
    assert rule is not None
    result, message = quality.change_version(
        entry_id=rule["id"], target_version="v3", expected_version="v2"
    )
    assert result is None
    assert "尚未评审" in message


# ---------------------------------------------------------------------------
# 清洗换版联动
# ---------------------------------------------------------------------------
def test_cleanrule_output_change_marks_affected_range(
    cleanrule: CleanruleService, quality: QualityService
) -> None:
    """清洗 v2 新增 pay_status：只重评受影响字段，旧版本通过结论不继承。"""
    target = cleanrule.find_version("CLEAN_PAY_AMOUNT", "v2")
    assert target is not None
    _, message = cleanrule.run_action(target["id"], "评审通过")
    assert "已评审通过" in message

    result, message = cleanrule.change_version(
        entry_id=target["id"],
        target_version="v2",
        expected_version="v1",
        affected_from="2026-10-01",
        affected_to="2026-10-20",
    )
    assert result is not None, message
    assert result["affected_range"] == "DWD_PAY_ORDER|2026-10-01~2026-10-20|pay_status"

    status_rule = engine.latest_evaluation("QUAL_PAY_STATUS", "v1", active_only=True)
    assert status_rule is not None
    assert status_rule["state"] == engine.STATE_ZERO
    assert status_rule["conclusion"] == engine.PENDING
    assert status_rule["affected_range"] == result["affected_range"]

    summary = quality.table_summary("DWD_PAY_ORDER")
    assert summary["阻断结论"] == engine.PENDING
    assert summary["受影响区间"] == result["affected_range"]

    # 清洗规则页与质量页复用同一份结论。
    decorated = cleanrule.get_entry(target["id"])
    assert decorated["质量结论"] == summary["质量结论"]
    assert decorated["阻断结论"] == summary["阻断结论"]


def test_cleanrule_unreviewed_version_blocked(cleanrule: CleanruleService) -> None:
    """未评审的清洗版本不能生效。"""
    target = cleanrule.find_version("CLEAN_PAY_AMOUNT", "v2")
    assert target is not None
    result, message = cleanrule.change_version(
        entry_id=target["id"], target_version="v2", expected_version="v1"
    )
    assert result is None
    assert "尚未评审" in message


def test_cleanrule_expected_version_conflict(cleanrule: CleanruleService) -> None:
    target = cleanrule.find_version("CLEAN_PAY_AMOUNT", "v2")
    assert target is not None
    cleanrule.run_action(target["id"], "评审通过")
    result, message = cleanrule.change_version(
        entry_id=target["id"], target_version="v2", expected_version="v2"
    )
    assert result is None
    assert "换版冲突" in message


def test_table_summary_counts_only_effective_versions(
    quality: QualityService, cleanrule: CleanruleService
) -> None:
    """表级汇总只统计当前生效版本，历史版本的阻断/通过不重复计数。"""
    rule = _approve(quality, "QUAL_PAY_AMOUNT", "v3")
    quality.change_version(entry_id=rule["id"], target_version="v3", expected_version="v2")
    summary = quality.table_summary("DWD_PAY_ORDER")
    assert summary["阻断规则数"] == 1
    assert summary["阻断结论"] == engine.BLOCK


def test_cleanrule_history_row_does_not_carry_current_conclusion(
    quality: QualityService, cleanrule: CleanruleService
) -> None:
    """历史清洗版本不挂当前质量结论，只有生效版本展示同一份结论。"""
    target = cleanrule.find_version("CLEAN_PAY_AMOUNT", "v2")
    assert target is not None
    cleanrule.run_action(target["id"], "评审通过")
    cleanrule.change_version(
        entry_id=target["id"],
        target_version="v2",
        expected_version="v1",
        affected_from="2026-10-01",
        affected_to="2026-10-20",
    )
    current = cleanrule.get_entry(target["id"])
    history = cleanrule.get_entry(cleanrule.find_version("CLEAN_PAY_AMOUNT", "v1")["id"])
    assert current["质量结论"] is not None
    assert history["质量结论"] is None
    assert current["受影响区间"] == "DWD_PAY_ORDER|2026-10-01~2026-10-20|pay_status"


def test_duplicate_quality_version_registration_rejected(quality: QualityService) -> None:
    """同一规则同一版本不能重复登记。"""
    entry, missing = quality.create_entry(
        {"规则编号": "QUAL_PAY_AMOUNT", "校验表名": "DWD_PAY_ORDER", "目标字段": "pay_amount", "规则版本": "v2"}
    )
    assert entry is None and missing == []


def test_sample_hash_and_snapshot_fields(quality: QualityService) -> None:
    """采样快照保留 sample_hash / sample_size / bad_rows / boundary_value / evaluated_at。"""
    rule = _approve(quality, "QUAL_PAY_AMOUNT", "v3")
    quality.change_version(entry_id=rule["id"], target_version="v3", expected_version="v2")
    snapshot = quality.detail("QUAL_PAY_AMOUNT", "v3")["采样快照"][-1]
    for field in ("sample_hash", "sample_size", "bad_rows", "boundary_value", "evaluated_at"):
        assert field in snapshot
    assert snapshot["sample_size"] == 1000
    assert snapshot["bad_rows"] == 24
    assert snapshot["boundary_value"] == 2.0


def test_no_cross_version_reuse_when_plan_changed(quality: QualityService) -> None:
    """采样比例变化后不能复用旧样本，避免旧结论挂到新版本。"""
    rule = _approve(quality, "QUAL_PAY_AMOUNT", "v3")
    rule["采样比例"] = 0.2
    result, message = quality.change_version(
        entry_id=rule["id"], target_version="v3", expected_version="v2"
    )
    assert result is not None, message
    assert result["reuse_mode"] == engine.REUSE_FULL_RERUN
    assert result["reused_sample"] is False


def test_cross_version_reuse_only_from_effective_version(quality: QualityService) -> None:
    """跨版本复用只认换版前生效版本的快照，被替换版本的样本不再参与。"""
    rule = _approve(quality, "QUAL_PAY_AMOUNT", "v3")
    result, message = quality.change_version(
        entry_id=rule["id"], target_version="v3", expected_version="v2"
    )
    assert result is not None, message
    assert result["reused_from"] == "QUAL_PAY_AMOUNT|v2|DWD_PAY_ORDER|pay_amount|0.1"


def test_rule_row_sample_not_inherited_across_versions(quality: QualityService) -> None:
    """规则行上的样本只属于上报时的版本，新版本不继承旧版本的样本数值。"""
    rule = _approve(quality, "QUAL_PAY_AMOUNT", "v3")
    # 模拟上报时 v2 行上的样本数值；v3 判定不应读到这份样本。
    rule["sample_size"] = 999
    rule["bad_rows"] = 1
    result, message = quality.change_version(
        entry_id=rule["id"], target_version="v3", expected_version="v2"
    )
    assert result is not None, message
    # 复用 v2 快照（1000/24），而不是行上的 999/1。
    assert result["sample_size"] == 1000
    assert result["bad_rows"] == 24
