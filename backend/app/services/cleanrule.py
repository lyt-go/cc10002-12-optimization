"""清洗规则业务规则：版本换版、输出字段变更与质量联动。

清洗规则换版只允许一个已评审版本生效；如果输出字段发生变化，
在发布同一批次里标记受影响区间，并通知质量服务只重跑必要样本。
"""
from __future__ import annotations

from typing import Any

from app.services import quality_engine as engine
from app.services.versioning import stable_hash
from app.store import store

MODULE = "cleanrule"
CLEAN_LEDGER = "cleanrule_version_ledger"
CLEAN_RELEASE = "cleanrule_release"
REQUIRED_FIELDS = ["规则编号", "输入表名", "空值策略"]
STATUS_ORDER = ["草稿", "规则评审中", "已启用", "已废弃"]
ACTION_RULES = {
    "提交规则评审": "规则评审中",
    "评审通过": "规则评审中",
    "启用规则": "已启用",
    "废弃规则": "已废弃",
}
LEGAL_ACTIONS = set(ACTION_RULES)


def split_fields(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "")
    for sep in ("，", ",", "、", "|"):
        text = text.replace(sep, " ")
    return [item for item in text.split() if item]


class CleanruleService:
    # ------------------------------------------------------------------
    # 列表 / 明细 / 登记
    # ------------------------------------------------------------------
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        table: str | None = None,
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
                or keyword in str(row.get("输入表名", ""))
                or keyword in str(row.get("空值策略", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if table:
            rows = [row for row in rows if str(row.get("输入表名")) == table]
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
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["规则编号"] = str(values.get("规则编号")).strip()
        entry["规则版本"] = str(values.get("规则版本") or "v1").strip()
        entry["输出字段"] = split_fields(values.get("输出字段"))
        entry["reviewed"] = False
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        self._ensure_ledger(entry, status="草稿", reviewed=False)
        return entry, []

    # ------------------------------------------------------------------
    # 原有动作
    # ------------------------------------------------------------------
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"清洗规则 {entry_id} 不存在或已归档"
        if action not in LEGAL_ACTIONS:
            return None, f"动作「{action}」不属于清洗规则可执行范围"

        if action == "提交规则评审":
            ledger = self._ensure_ledger(entry, status="评审中", reviewed=False)
            ledger["status"] = "评审中"
            entry["status"] = "规则评审中"
            entry["pending"] = True
            entry["reviewed"] = False
            return entry, f"清洗规则 {entry['规则编号']} {entry['规则版本']} 已提交评审"
        if action == "评审通过":
            ledger = self._ensure_ledger(entry, status="待生效", reviewed=True)
            ledger["status"] = "待生效"
            ledger["reviewed"] = True
            entry["status"] = "规则评审中"
            entry["pending"] = True
            entry["reviewed"] = True
            return entry, f"清洗规则 {entry['规则编号']} {entry['规则版本']} 已评审通过，可换版生效"
        if action == "启用规则":
            return self._release(entry)
        entry["status"] = "已废弃"
        entry["pending"] = False
        entry["abnormal"] = False
        ledger = self._ensure_ledger(entry)
        ledger["status"] = "已废弃"
        return entry, f"清洗规则 {entry['规则编号']} {entry['规则版本']} 已废弃"

    def _release(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        rule_no = str(entry.get("规则编号"))
        version = str(entry.get("规则版本"))
        with engine.RELEASE_LOCK:
            ledger = self._ensure_ledger(entry)
            if not ledger.get("reviewed"):
                return None, f"清洗规则 {rule_no} {version} 尚未评审，不能作为生效版本"
            result, message, _ = self._publish(entry, trigger="启用规则")
            if result is None:
                return None, message
            entry["status"] = "已启用"
            entry["pending"] = False
            entry["abnormal"] = result["block_status"] == engine.BLOCK
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
        affected_from: str | None = None,
        affected_to: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"清洗规则 {entry_id} 不存在或已归档"
        rule_no = str(entry.get("规则编号"))
        target = self.find_version(rule_no, target_version)
        if target is None:
            return None, f"清洗规则 {rule_no} 版本 {target_version} 不存在"
        with engine.RELEASE_LOCK:
            current = self.current_effective(rule_no)
            if expected_version is not None and current is not None:
                if str(current.get("规则版本")) != str(expected_version):
                    return None, (
                        f"换版冲突：持久化已生效版本为 {current.get('规则版本')}，"
                        f"提交的 expected_version={expected_version} 不匹配"
                    )
            ledger = self._ensure_ledger(target)
            if not ledger.get("reviewed"):
                return None, f"清洗规则 {rule_no} 版本 {target_version} 尚未评审，不能生效"
            result, message, _ = self._publish(
                target,
                trigger="换版发布",
                affected_from=affected_from,
                affected_to=affected_to,
            )
            return result, message

    def _publish(
        self,
        entry: dict[str, Any],
        *,
        trigger: str,
        affected_from: str | None = None,
        affected_to: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]]]:
        rule_no = str(entry.get("规则编号"))
        version = str(entry.get("规则版本"))
        batch = engine.batch_id(rule_no, version)
        current = self.current_effective(rule_no)
        old_version = str(current.get("规则版本")) if current else None
        old_entry = self.find_version(rule_no, old_version) if old_version else None

        old_fields = split_fields((old_entry or {}).get("输出字段"))
        new_fields = split_fields(entry.get("输出字段"))
        changed_fields = [field for field in new_fields if field not in old_fields]
        output_changed = bool(changed_fields)
        released_at = engine.now_iso()
        default_from = str(entry.get("生效日期") or old_entry.get("生效日期") if old_entry else entry.get("生效日期") or "")
        start = affected_from or default_from or released_at[:10]
        end = affected_to or start
        if start > end:
            start, end = end, start
        affected_range = (
            f"{entry.get('输入表名')}|{start}~{end}|{'~'.join(changed_fields)}"
            if output_changed
            else None
        )

        # 版本账本：只有已评审版本可生效，旧版本置为已替换。
        ledger = self._ensure_ledger(entry)
        if current is not None and str(current.get("规则版本")) != version:
            current["status"] = "已替换"
            current["replaced_by"] = version
            current["replaced_at"] = released_at
        ledger["status"] = "已生效"
        ledger["reviewed"] = True
        ledger["released_at"] = released_at
        ledger["release_batch"] = batch
        ledger["content_hash"] = self.content_hash(entry)
        ledger["output_fields"] = new_fields
        ledger["affected_range"] = affected_range
        entry["reviewed"] = True

        # 发布记录：输出字段变化时记录受影响区间，供质量页复用。
        release = self._release_record(rule_no)
        release.update(
            {
                "规则编号": rule_no,
                "生效版本": version,
                "候选版本": None,
                "发布状态": "已生效",
                "output_changed": output_changed,
                "affected_range": affected_range,
                "affected_fields": changed_fields,
                "released_at": released_at,
                "release_batch": batch,
            }
        )

        evaluations: list[dict[str, Any]] = []
        if output_changed:
            from app.services.quality import QualityService  # 延迟导入，避免循环

            evaluations = QualityService().re_evaluate_affected(
                table=str(entry.get("输入表名")),
                fields=changed_fields,
                affected_from=start,
                affected_to=end,
                batch=batch,
                clean_version=version,
                old_version=old_version,
            )

        engine.write_audit(
            table=MODULE,
            change_type="换版发布",
            before=old_version,
            after=version,
            reason=f"{trigger}：输出字段 {'、'.join(changed_fields) or '未变化'}，受影响区间 {affected_range or '无'}",
            batch=batch,
            abnormal=any(item["block_status"] == engine.BLOCK for item in evaluations),
        )
        message = f"清洗规则 {rule_no} 已由 {old_version or '无'} 换版到 {version}"
        if output_changed:
            message += f"，输出字段变化 {', '.join(changed_fields)}，受影响区间 {affected_range}"
            message += f"，质量联动重评 {len(evaluations)} 条规则"
        return ({"version": version, "affected_range": affected_range, "evaluations": evaluations}), message, evaluations

    # ------------------------------------------------------------------
    # 质量回写：阻断结论同步到清洗页
    # ------------------------------------------------------------------
    def mark_quality_impact(
        self,
        *,
        table: str,
        field: str,
        version: str,
        conclusion: str,
        block_status: str,
        batch: str,
    ) -> None:
        for entry in [row for row in store.rows(MODULE) if str(row.get("输入表名")) == table]:
            entry["质量结论"] = conclusion
            entry["阻断结论"] = block_status
            entry["质量判定版本"] = version
            entry["质量判定批次"] = batch
            if block_status == engine.BLOCK:
                entry["abnormal"] = True

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _decorate(self, row: dict[str, Any]) -> dict[str, Any]:
        rule_no = str(row.get("规则编号"))
        version = str(row.get("规则版本"))
        current = self.current_effective(rule_no)
        is_current = current is not None and str(current.get("规则版本")) == version
        release = self._release_record(rule_no) if is_current else {}
        from app.services.quality import QualityService  # 延迟导入，避免循环

        # 历史版本行不挂当前质量结论：结论只属于当时生效的版本。
        summary = QualityService().table_summary(str(row.get("输入表名"))) if is_current else {}
        return {
            "当前生效版本": str(current.get("规则版本")) if current else None,
            "版本是否已生效": is_current,
            "版本是否被替换": current is not None and not is_current,
            "输出字段是否变化": bool(release.get("output_changed")),
            "受影响区间": release.get("affected_range"),
            "质量结论": summary.get("质量结论"),
            "阻断结论": summary.get("阻断结论"),
            "阻断规则数": summary.get("阻断规则数"),
            "待判定规则数": summary.get("待判定规则数"),
            "判定批次": summary.get("判定批次"),
        }

    def current_effective(self, rule_no: str) -> dict[str, Any] | None:
        rows = [
            row for row in store.rows(CLEAN_LEDGER) if row.get("规则编号") == rule_no and row.get("status") == "已生效"
        ]
        if not rows:
            return None
        return max(rows, key=lambda row: str(row.get("released_at") or ""))

    def find_version(self, rule_no: str, version: str | None = None) -> dict[str, Any] | None:
        rows = [row for row in store.rows(MODULE) if row.get("规则编号") == rule_no]
        if version is None:
            return rows[0] if rows else None
        for row in rows:
            if str(row.get("规则版本")) == str(version):
                return row
        return None

    def _ensure_ledger(
        self, entry: dict[str, Any], *, status: str = "待评审", reviewed: bool | None = None
    ) -> dict[str, Any]:
        rule_no = str(entry.get("规则编号"))
        version = str(entry.get("规则版本"))
        for row in store.rows(CLEAN_LEDGER):
            if row.get("规则编号") == rule_no and str(row.get("规则版本")) == version:
                return row
        rows = store.rows(CLEAN_LEDGER)
        row = {
            "id": max((int(item.get("id", 0)) for item in rows), default=0) + 1,
            "规则编号": rule_no,
            "规则版本": version,
            "content_hash": self.content_hash(entry),
            "status": status,
            "reviewed": bool(entry.get("reviewed")) if reviewed is None else reviewed,
            "released_at": None,
            "replaced_by": None,
            "reviewed_by": "数据治理评审组" if reviewed else None,
            "input_table": entry.get("输入表名"),
            "output_fields": split_fields(entry.get("输出字段")),
        }
        rows.append(row)
        return row

    def _release_record(self, rule_no: str) -> dict[str, Any]:
        rows = store.rows(CLEAN_RELEASE)
        for row in rows:
            if row.get("规则编号") == rule_no:
                return row
        row = {"id": max((int(item.get("id", 0)) for item in rows), default=0) + 1, "规则编号": rule_no}
        rows.append(row)
        return row

    @staticmethod
    def content_hash(entry: dict[str, Any]) -> str:
        return stable_hash(
            {
                "input": entry.get("输入表名"),
                "null": entry.get("空值策略"),
                "format": entry.get("格式表达式"),
                "threshold": entry.get("异常阈值"),
                "output": split_fields(entry.get("输出字段")),
            }
        )

    def version_options(self, rule_no: str) -> list[dict[str, Any]]:
        options = []
        for ledger in [
            row for row in store.rows(CLEAN_LEDGER) if row.get("规则编号") == rule_no
        ]:
            options.append(
                {
                    "规则版本": ledger.get("规则版本"),
                    "审核状态": ledger.get("status"),
                    "已评审": bool(ledger.get("reviewed")),
                    "已生效": ledger.get("status") == "已生效",
                    "受影响区间": ledger.get("affected_range"),
                    "输出字段": ledger.get("output_fields"),
                }
            )
        return sorted(options, key=lambda item: str(item["规则版本"]))

    def detail(self, rule_no: str, version: str | None = None) -> dict[str, Any] | None:
        entry = self.find_version(rule_no, version)
        if entry is None:
            return None
        from app.services.quality import QualityService

        return {
            "规则": {**entry, **self._decorate(entry)},
            "版本列表": self.version_options(rule_no),
            "质量结论": QualityService().table_summary(str(entry.get("输入表名"))),
        }
