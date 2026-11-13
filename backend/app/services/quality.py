"""质量校验业务规则：版本换版、采样复用、阻断结论与清洗联动。

对外列表/明细沿用原有结构；换版链路先写账本再判定，判定与审计同批次。
"""
from __future__ import annotations

from typing import Any

from app.services import quality_engine as engine
from app.services.versioning import (
    content_hash,
    normalize_policy,
    sampling_hash,
    to_float,
    to_ratio,
    verdict_hash,
)
from app.store import store

MODULE = "quality"
REQUIRED_FIELDS = ["规则编号", "校验表名", "目标字段"]
STATUS_ORDER = ["草稿", "规则评审中", "已启用", "已废弃"]
ACTION_RULES = {
    "提交规则评审": "规则评审中",
    "评审通过": "规则评审中",
    "启用规则": "已启用",
    "废弃规则": "已废弃",
}
LEGAL_ACTIONS = set(ACTION_RULES)


class QualityService:
    # ------------------------------------------------------------------
    # 列表 / 明细 / 登记
    # ------------------------------------------------------------------
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        table: str | None = None,
        field: str | None = None,
        version: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [dict(row) for row in store.rows(MODULE)]
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("规则编号", ""))
                or keyword in str(row.get("校验表名", ""))
                or keyword in str(row.get("目标字段", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if table:
            rows = [row for row in rows if str(row.get("校验表名")) == table]
        if field:
            rows = [row for row in rows if field in str(row.get("目标字段", ""))]
        if version:
            rows = [row for row in rows if str(row.get("规则版本")) == str(version)]
        for row in rows:
            row.update(self._decorate(row))
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        row = dict(entry)
        row.update(self._decorate(row))
        return row

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rule_no = str(values.get("规则编号")).strip()
        version = str(values.get("规则版本") or "v1").strip()
        if self.find_version(rule_no, version) is not None:
            return None, []  # 版本重复：由路由转成可读提示
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update(
            {
                "规则编号": rule_no,
                "校验表名": str(values.get("校验表名")).strip(),
                "目标字段": str(values.get("目标字段")).strip(),
                "校验表达式": values.get("校验表达式") or f"{values.get('目标字段')} IS NOT NULL",
                "阈值下界": to_float(values.get("阈值下界")),
                "阈值上界": to_float(values.get("阈值上界")),
                "采样比例": to_ratio(values.get("采样比例")),
                "阻断级别": values.get("阻断级别") or "阻断",
                "边界策略": normalize_policy(values.get("边界策略")),
                "规则版本": version,
                "status": STATUS_ORDER[0],
                "pending": True,
                "abnormal": False,
                "reviewed": False,
            }
        )
        rows.append(entry)
        engine.ensure_ledger(entry, status="草稿", reviewed=False)
        return entry, []

    # ------------------------------------------------------------------
    # 原有动作
    # ------------------------------------------------------------------
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"质量规则 {entry_id} 不存在或已归档"
        if action not in LEGAL_ACTIONS:
            return None, f"动作「{action}」不属于质量校验可执行范围"

        if action == "提交规则评审":
            ledger = engine.ensure_ledger(entry, status="评审中", reviewed=False)
            ledger["status"] = "评审中"
            entry["status"] = "规则评审中"
            entry["pending"] = True
            entry["reviewed"] = False
            return entry, f"质量规则 {entry['规则编号']} {entry['规则版本']} 已提交评审"
        if action == "评审通过":
            ledger = engine.mark_reviewed(entry)
            entry["status"] = "规则评审中"
            entry["pending"] = True
            entry["reviewed"] = True
            return entry, f"质量规则 {entry['规则编号']} {entry['规则版本']} 已评审通过，可换版生效"
        if action == "启用规则":
            return self._release(entry)
        entry["status"] = "已废弃"
        entry["pending"] = False
        entry["abnormal"] = False
        return entry, f"质量规则 {entry['规则编号']} {entry['规则版本']} 已废弃"

    def _release(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """启用即换版：只允许一个已评审版本生效，判定与发布同批次。"""
        rule_no = str(entry.get("规则编号"))
        version = str(entry.get("规则版本"))
        with engine.RELEASE_LOCK:
            ledger = engine.get_ledger(rule_no, version) or engine.ensure_ledger(entry)
            if not ledger.get("reviewed"):
                return None, f"质量规则 {rule_no} {version} 尚未评审，不能作为生效版本"
            result, message = self._evaluate_and_release(
                rule=entry,
                ledger=ledger,
                trigger="启用规则",
                batch=engine.batch_id(rule_no, version),
            )
            if result is None:
                return None, message
            entry["status"] = "已启用"
            entry["pending"] = False
            entry["abnormal"] = result["block_status"] in {engine.BLOCK, engine.WARN}
            return entry, message

    # ------------------------------------------------------------------
    # 换版主链路
    # ------------------------------------------------------------------
    def change_version(
        self,
        *,
        entry_id: int,
        target_version: str,
        expected_version: str | None = None,
        force_rerun: bool = False,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"质量规则 {entry_id} 不存在或已归档"
        rule_no = str(entry.get("规则编号"))
        target = self.find_version(rule_no, target_version)
        if target is None:
            return None, f"质量规则 {rule_no} 版本 {target_version} 不存在"

        with engine.RELEASE_LOCK:
            current = engine.current_effective(rule_no)
            if expected_version is not None and current is not None:
                if str(current.get("规则版本")) != str(expected_version):
                    return None, (
                        f"换版冲突：持久化已生效版本为 {current.get('规则版本')}，"
                        f"提交的 expected_version={expected_version} 不匹配"
                    )
            ledger = engine.get_ledger(rule_no, target_version)
            if ledger is None:
                return None, f"质量规则 {rule_no} 版本 {target_version} 未进入版本账本"
            if not ledger.get("reviewed"):
                return None, f"质量规则 {rule_no} 版本 {target_version} 尚未评审，不能生效"
            result, message = self._evaluate_and_release(
                rule=target,
                ledger=ledger,
                trigger="换版重评",
                batch=engine.batch_id(rule_no, target_version),
                force_rerun=force_rerun,
            )
            if result is not None:
                target["status"] = "已启用"
                target["pending"] = False
                target["abnormal"] = result["block_status"] in {engine.BLOCK, engine.WARN}
            return result, message

    def _evaluate_and_release(
        self,
        *,
        rule: dict[str, Any],
        ledger: dict[str, Any],
        trigger: str,
        batch: str,
        force_rerun: bool = False,
    ) -> tuple[dict[str, Any] | None, str]:
        rule_no = str(rule.get("规则编号"))
        version = str(rule.get("规则版本"))
        failure_state = str(rule.get("last_state") or "")

        # 1) 选取可复用样本：内容/阻断级别变化立即失效，阈值变化只重评边界。
        snapshot, reuse_mode, reason = self._resolve_reuse(rule, force_rerun)

        if force_rerun:
            reuse_mode, reason, snapshot = engine.REUSE_FULL_RERUN, "已要求强制重采", None
        if failure_state in engine.FAILURE_STATES:
            reuse_mode, reason, snapshot = engine.REUSE_FULL_RERUN, f"上次状态为{failure_state}，不能沿用", None

        sample_size, bad_rows = self._sample_of(rule, snapshot)
        bad_rate = round(bad_rows / sample_size * 100, 6) if sample_size else 0.0

        verdict = engine.apply_block_level(
            engine.judge_threshold(
                bad_rate=bad_rate,
                sample_size=sample_size,
                lower=to_float(rule.get("阈值下界")),
                upper=to_float(rule.get("阈值上界")),
                policy=normalize_policy(rule.get("边界策略")),
                failure_state=failure_state if failure_state in engine.FAILURE_STATES else None,
            ),
            rule.get("阻断级别"),
        )

        # 2) 同版本旧结论先失效，新判定按当前版本落库；其他版本原样保留。
        previous = engine.latest_evaluation(rule_no, version, active_only=True)
        if previous is not None:
            engine.invalidate_version(rule_no, version, batch=batch, reason="同版本重新判定，旧结论失效")

        snapshot_row = engine.record_snapshot(
            rule=rule,
            sample_size=sample_size,
            bad_rows=bad_rows,
            bad_rate=bad_rate,
            lower=to_float(rule.get("阈值下界")),
            upper=to_float(rule.get("阈值上界")),
            verdict=verdict,
            reused_from=snapshot,
        ) if verdict["state"] != engine.STATE_ZERO else None

        evaluation = engine.record_evaluation(
            rule=rule,
            ledger=ledger,
            batch=batch,
            verdict=verdict,
            sample_size=sample_size,
            bad_rows=bad_rows,
            bad_rate=bad_rate,
            snapshot=snapshot_row,
            reused_from=snapshot,
            reuse_mode=reuse_mode,
            reason=reason,
            trigger=trigger,
            affected_range=rule.get("affected_range"),
        )

        # 3) 版本发布 + 审计 + 两页联动，都在同一批次里完成。
        engine.release_ledger(rule, batch=batch)
        self._remember_sample(rule, sample_size, bad_rows)
        engine.write_audit(
            table=MODULE,
            change_type="换版判定",
            before=(previous or {}).get("sample_hash"),
            after=evaluation["sample_hash"],
            reason=f"{batch} {rule_no} {version} {trigger}：{evaluation['conclusion']}（{reason}）",
            batch=batch,
            abnormal=evaluation["block_status"] in {engine.BLOCK, engine.WARN},
        )
        engine.write_audit(
            table=engine.QUALITY_LEDGER,
            change_type="版本生效",
            before=(previous or {}).get("规则版本"),
            after=f"{rule_no} {version} {evaluation['conclusion']}",
            reason=f"复用模式：{reuse_mode}",
            batch=batch,
            abnormal=False,
        )
        self._notify_cleanrule(rule, evaluation, batch)
        message = (
            f"{rule_no} {version} 判定为{evaluation['conclusion']}"
            f"（异常率 {bad_rate:.2f}%，样本 {sample_size}，模式：{reuse_mode}，边界：{evaluation['boundary_side'] or '未命中'}）"
        )
        return evaluation, message

    def _sample_of(
        self, rule: dict[str, Any], snapshot: dict[str, Any] | None
    ) -> tuple[int, int]:
        """规则行上的样本只属于上报时的版本，不能跨版本继承。"""
        if snapshot is not None:
            return int(snapshot.get("sample_size") or 0), int(snapshot.get("bad_rows") or 0)
        if self._reported_version(rule) != str(rule.get("规则版本")):
            return 0, 0
        return int(rule.get("sample_size") or 0), int(rule.get("bad_rows") or 0)

    @staticmethod
    def _reported_version(rule: dict[str, Any]) -> str:
        return str(rule.get("sample_version") or "")

    def _remember_sample(self, rule: dict[str, Any], sample_size: int, bad_rows: int) -> None:
        """把行上的样本和它所属版本一起记住，避免换版后误用。"""
        rule["sample_size"] = int(sample_size)
        rule["bad_rows"] = int(bad_rows)
        rule["sample_version"] = str(rule.get("规则版本"))

    def _resolve_reuse(
        self,
        rule: dict[str, Any],
        force_rerun: bool,
    ) -> tuple[dict[str, Any] | None, str, str]:
        """复用优先级：同版本整版复用 > 跨版本同计划样本重评 > 全量重采。"""
        if force_rerun:
            return None, engine.REUSE_FULL_RERUN, "已要求强制重采"
        plan = sampling_hash(rule)
        content = content_hash(rule)
        verdict = verdict_hash(rule)
        rule_no = str(rule.get("规则编号"))
        version = str(rule.get("规则版本"))
        block_level = str(rule.get("阻断级别") or "")

        same_version = sorted(
            engine.snapshots_for(rule_no, version),
            key=lambda row: (str(row.get("evaluated_at") or ""), int(row.get("id", 0))),
            reverse=True,
        )
        for candidate in same_version:
            if str(candidate.get("sampling_hash") or "") != plan:
                continue
            if str(candidate.get("content_hash") or "") != content:
                continue
            if str(candidate.get("block_level") or "") != block_level:
                # 阻断级别变化：旧样本结论立即失效，不允许直接沿用。
                return None, engine.REUSE_FULL_RERUN, "阻断级别变化，旧结论立即失效，重新采样"
            if str(candidate.get("verdict_hash") or "") == verdict:
                return candidate, engine.REUSE_FULL, "采样与判定口径均未变化，整版复用"
            return candidate, engine.REUSE_SAMPLE, "采样口径未变，复用样本只重评阈值边界"

        effective = engine.current_effective(rule_no)
        effective_version = str((effective or {}).get("规则版本") or "")
        cross_version = sorted(
            [
                row
                for row in engine.snapshots_for(rule_no)
                if str(row.get("规则版本")) != version
                # 只认换版前那个生效版本的快照：被替换版本的样本已经失去上下文。
                and str(row.get("规则版本")) == effective_version
            ],
            key=lambda row: (str(row.get("evaluated_at") or ""), int(row.get("id", 0))),
            reverse=True,
        )
        for candidate in cross_version:
            if str(candidate.get("sampling_hash") or "") != plan:
                continue
            if str(candidate.get("content_hash") or "") != content:
                continue
            if str(candidate.get("block_level") or "") != block_level:
                return None, engine.REUSE_FULL_RERUN, "阻断级别变化，旧结论立即失效，重新采样"
            if str(candidate.get("verdict_hash") or "") == verdict:
                return candidate, engine.REUSE_FULL, "版本内无样本，跨版本同口径整版复用"
            return candidate, engine.REUSE_SAMPLE, "版本内无样本，跨版本同口径复用样本重评"
        return None, engine.REUSE_FULL_RERUN, "采样口径或规则内容变化，旧结论失效，重新采样"

    # ------------------------------------------------------------------
    # 采样上报：明确状态，失败/中断/超时不能沿用上次通过
    # ------------------------------------------------------------------
    def report_sample(
        self,
        entry_id: int,
        *,
        sample_size: int,
        bad_rows: int,
        state: str = engine.STATE_DONE,
        reason: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"质量规则 {entry_id} 不存在或已归档"
        state = state if state in engine.LEGAL_STATES else engine.STATE_SAMPLE_FAILED
        entry["sample_size"] = max(int(sample_size), 0)
        entry["bad_rows"] = max(int(bad_rows), 0)
        entry["sample_version"] = str(entry.get("规则版本"))
        entry["last_state"] = state
        entry["last_sample_at"] = engine.now_iso()
        batch = engine.batch_id(str(entry.get("规则编号")), str(entry.get("规则版本")))
        engine.write_audit(
            table=MODULE,
            change_type="采样上报",
            before=None,
            after=f"{entry.get('规则编号')} {entry.get('规则版本')} {state} 样本 {entry['sample_size']} 异常 {entry['bad_rows']}",
            reason=reason or "采样结果上报",
            batch=batch,
            abnormal=state in engine.FAILURE_STATES,
        )
        if state in engine.FAILURE_STATES:
            return entry, f"采样状态为{state}，需重新采样，不能沿用上次通过"
        return entry, f"采样状态为{state}，样本 {entry['sample_size']}，异常 {entry['bad_rows']}"

    # ------------------------------------------------------------------
    # 清洗联动：受影响区间内重评，只跑必要样本
    # ------------------------------------------------------------------
    def re_evaluate_affected(
        self,
        *,
        table: str,
        fields: list[str],
        affected_from: str,
        affected_to: str,
        batch: str,
        clean_version: str,
        old_version: str | None = None,
    ) -> list[dict[str, Any]]:
        """清洗换版改了输出字段：只重评该表受影响字段上的规则。

        受影响区间内没有历史样本的规则（例如新增字段）判为待判定，
        不能把旧版本通过结论挂在清洗新版本上。
        """
        results: list[dict[str, Any]] = []
        affected_range = f"{table}|{affected_from}~{affected_to}|{'~'.join(fields)}"
        rule_nos = sorted(
            {
                str(row.get("规则编号"))
                for row in store.rows(MODULE)
                if str(row.get("校验表名")) == table
            }
        )
        for rule_no in rule_nos:
            with engine.RELEASE_LOCK:
                current = engine.current_effective(rule_no)
                if current is None:
                    continue  # 没有生效版本就无可继承、也无可重评的结论
                version = str(current.get("规则版本"))
                rule = self.find_version(rule_no, version)
                if rule is None:
                    continue
                target = str(rule.get("目标字段"))
                if fields and target not in fields:
                    continue
                reusable = [
                    row
                    for row in engine.snapshots_for(rule_no, version)
                    if int(row.get("sample_size") or 0) > 0
                ]
                rule["clean_version"] = clean_version
                rule["affected_range"] = affected_range
                rule["affected_from"] = affected_from
                rule["affected_to"] = affected_to

                if not reusable:
                    # 新增输出字段在旧版本里根本不存在样本，旧结论不能继承。
                    sample_size, bad_rows = 0, 0
                    verdict = engine.judge_threshold(
                        bad_rate=0.0,
                        sample_size=0,
                        lower=to_float(rule.get("阈值下界")),
                        upper=to_float(rule.get("阈值上界")),
                        policy=normalize_policy(rule.get("边界策略")),
                    )
                    verdict["reason"] = "清洗新增输出字段，受影响区间无历史样本，旧版本通过结论不继承"
                    snapshot, reuse_mode = None, engine.REUSE_AFFECTED
                else:
                    # 受影响区间之外不重跑：按快照覆盖周期折算样本，只重评必要样本。
                    source = reusable[-1]
                    sample_size = self._affected_sample_size(
                        int(source.get("sample_size") or 0),
                        affected_from,
                        affected_to,
                        str(source.get("period_from") or ""),
                        str(source.get("period_to") or ""),
                    )
                    bad_rows = self._affected_bad_rows(source, sample_size)
                    verdict = engine.apply_block_level(
                        engine.judge_threshold(
                            bad_rate=round(bad_rows / sample_size * 100, 6) if sample_size else 0.0,
                            sample_size=sample_size,
                            lower=to_float(rule.get("阈值下界")),
                            upper=to_float(rule.get("阈值上界")),
                            policy=normalize_policy(rule.get("边界策略")),
                        ),
                        rule.get("阻断级别"),
                    )
                    snapshot, reuse_mode = source, engine.REUSE_AFFECTED

                bad_rate = round(bad_rows / sample_size * 100, 6) if sample_size else 0.0
                snapshot_row = engine.record_snapshot(
                    rule=rule,
                    sample_size=sample_size,
                    bad_rows=bad_rows,
                    bad_rate=bad_rate,
                    lower=to_float(rule.get("阈值下界")),
                    upper=to_float(rule.get("阈值上界")),
                    verdict=verdict,
                    reused_from=snapshot,
                ) if sample_size else None
                evaluation = engine.record_evaluation(
                    rule=rule,
                    ledger=engine.get_ledger(rule_no, version) or {},
                    batch=batch,
                    verdict=verdict,
                    sample_size=sample_size,
                    bad_rows=bad_rows,
                    bad_rate=bad_rate,
                    snapshot=snapshot_row,
                    reused_from=snapshot,
                    reuse_mode=reuse_mode,
                    reason=f"清洗 {old_version or ''}→{clean_version} 改动输出字段 {affected_range}，只重跑受影响区间样本",
                    trigger="清洗换版联动",
                    affected_range=affected_range,
                )
                results.append(evaluation)
                engine.write_audit(
                    table=MODULE,
                    change_type="清洗换版联动",
                    before=old_version,
                    after=f"{rule_no} {version} {verdict['conclusion']} {affected_range}",
                    reason="清洗规则换版改变输出字段，质量结论按受影响区间重评",
                    batch=batch,
                    abnormal=verdict["block_status"] == engine.BLOCK,
                )
        return results

    @staticmethod
    def _affected_sample_size(
        total: int,
        affected_from: str,
        affected_to: str,
        period_from: str = "",
        period_to: str = "",
    ) -> int:
        """按「受影响天数占快照覆盖天数」折算必要样本，不做全量重跑。"""
        from datetime import date

        try:
            start = date.fromisoformat(affected_from)
            end = date.fromisoformat(affected_to)
            cycle_start = date.fromisoformat(period_from) if period_from else start
            cycle_end = date.fromisoformat(period_to) if period_to else end
        except ValueError:
            return total
        if start > end:
            start, end = end, start
        cycle_days = (cycle_end - cycle_start).days + 1
        if cycle_days <= 0 or total <= 0:
            return 0
        overlap = min(end, cycle_end) - max(start, cycle_start)
        days = overlap.days + 1 if overlap.days >= 0 else 0
        return max(int(round(total * days / cycle_days)), 1) if days else 0

    @staticmethod
    def _affected_bad_rows(source: dict[str, Any] | None, affected: int) -> int:
        if source is None or affected <= 0:
            return 0
        rate = float(source.get("bad_rate") or 0.0) / 100
        return int(round(affected * rate))

    def _notify_cleanrule(self, rule: dict[str, Any], evaluation: dict[str, Any], batch: str) -> None:
        from app.services.cleanrule import CleanruleService  # 延迟导入，避免循环依赖

        CleanruleService().mark_quality_impact(
            table=str(rule.get("校验表名")),
            field=str(rule.get("目标字段")),
            version=str(rule.get("规则版本")),
            conclusion=str(evaluation.get("conclusion")),
            block_status=str(evaluation.get("block_status")),
            batch=batch,
        )

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------
    def find_version(self, rule_no: str, version: str | None = None) -> dict[str, Any] | None:
        rows = [row for row in store.rows(MODULE) if row.get("规则编号") == rule_no]
        if version is None:
            effective = engine.current_effective(rule_no)
            if effective is not None:
                for row in rows:
                    if str(row.get("规则版本")) == str(effective.get("规则版本")):
                        return row
            return rows[0] if rows else None
        for row in rows:
            if str(row.get("规则版本")) == str(version):
                return row
        return None

    def version_options(self, rule_no: str) -> list[dict[str, Any]]:
        options = []
        for ledger in engine.ledger_rows(rule_no):
            version = str(ledger.get("规则版本"))
            latest = engine.latest_evaluation(rule_no, version, active_only=True)
            options.append(
                {
                    "规则版本": version,
                    "审核状态": ledger.get("status"),
                    "已评审": bool(ledger.get("reviewed")),
                    "已生效": ledger.get("status") == "已生效",
                    "结论": (latest or {}).get("conclusion"),
                    "阻断结论": (latest or {}).get("block_status"),
                    "判定状态": (latest or {}).get("state"),
                    "复用模式": (latest or {}).get("reuse_mode"),
                    "最近判定时间": (latest or {}).get("evaluated_at"),
                }
            )
        return sorted(options, key=lambda item: item["规则版本"])

    def detail(self, rule_no: str, version: str | None = None) -> dict[str, Any] | None:
        rule = self.find_version(rule_no, version)
        if rule is None:
            return None
        chosen = version or str(rule.get("规则版本"))
        effective = engine.current_effective(rule_no)
        return {
            "规则": {**rule, **self._decorate(rule)},
            "版本列表": self.version_options(rule_no),
            "当前生效版本": str(effective.get("规则版本")) if effective else None,
            "采样快照": engine.snapshots_for(rule_no, chosen),
            "判定历史": engine.evaluations_for(rule_no, chosen, active_only=False),
        }

    def _decorate(self, row: dict[str, Any]) -> dict[str, Any]:
        rule_no = str(row.get("规则编号"))
        version = str(row.get("规则版本"))
        current = engine.current_effective(rule_no)
        latest = engine.latest_evaluation(rule_no, version, active_only=True)
        return {
            "当前生效版本": str(current.get("规则版本")) if current else None,
            "版本是否已生效": current is not None and str(current.get("规则版本")) == version,
            "版本是否被替换": current is not None and str(current.get("规则版本")) != version,
            "质量结论": (latest or {}).get("conclusion"),
            "采样结论": (latest or {}).get("conclusion"),
            "阻断结论": (latest or {}).get("block_status"),
            "判定状态": (latest or {}).get("state"),
            "判定批次": (latest or {}).get("批次"),
            "复用模式": (latest or {}).get("reuse_mode"),
            "受影响区间": (latest or {}).get("affected_range"),
            "采样快照": (engine.snapshots_for(rule_no, version) or [None])[-1],
        }

    # 供清洗规则页读取同一份质量结论（两页复用同一口径）。
    def table_summary(self, table: str) -> dict[str, Any]:
        rules = [row for row in store.rows(MODULE) if str(row.get("校验表名")) == table]
        summary: dict[str, Any] = {
            "影响规则数": len(rules),
            "阻断规则数": 0,
            "待判定规则数": 0,
            "质量结论": None,
            "阻断结论": None,
            "受影响区间": None,
            "判定批次": None,
        }
        worst = None
        for rule_no in sorted({str(row.get("规则编号")) for row in rules}):
            # 每个规则只看持久化生效版本的结论，历史版本不参与汇总。
            current = engine.current_effective(rule_no)
            if current is None:
                continue
            latest = engine.latest_evaluation(rule_no, str(current.get("规则版本")), active_only=True)
            if latest is None:
                continue
            if latest.get("block_status") == engine.BLOCK:
                summary["阻断规则数"] += 1
            if latest.get("conclusion") == engine.PENDING:
                summary["待判定规则数"] += 1
            if summary["受影响区间"] is None and latest.get("affected_range"):
                summary["受影响区间"] = latest["affected_range"]
            if worst is None or _severity(latest) >= _severity(worst):
                worst = latest
        if worst is not None:
            summary["质量结论"] = worst.get("conclusion")
            summary["阻断结论"] = worst.get("block_status")
            summary["判定批次"] = worst.get("批次")
        return summary


def _severity(evaluation: dict[str, Any]) -> int:
    """阻断 > 待判定 > 告警 > 通过：列表展示最严重的那条结论。"""
    return {
        engine.BLOCK: 3,
        engine.PENDING: 2,
        engine.WARN: 1,
        engine.PASS: 0,
    }.get(str(evaluation.get("block_status")), 0)
