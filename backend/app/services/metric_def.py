"""指标定义业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "metric_def"
LIST_FIELDS = ["指标编号", "指标名称", "计算公式", "统计粒度", "时间窗口", "基准取值", "精度位数", "口径版本"]
REQUIRED_FIELDS = ["指标编号", "指标名称", "计算公式"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
NEGATIVE_ACTIONS = []


class MetricDefService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("指标编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"指标定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于指标定义可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"指标定义已{action}"

    def get_forecast_results(self, entry_id: int) -> dict[str, Any] | None:
        """V030 跨模块联动：按指标编号读取预测结果表的指标标识，并核对周期位置。

        指标定义的时间窗口是校验基准：预测结果的历史窗口不能超过它，
        首个预测周期也必须是该历史窗口之后的第一个周期。
        """
        entry = self.get_entry(entry_id)
        if entry is None:
            return None
        metric_code = str(entry.get("指标编号") or "")
        declared_window = _parse_window(entry.get("时间窗口"))
        linked: list[dict[str, Any]] = []
        issues: list[str] = []
        model_ids: set[int] = set()
        for row in store.rows("forecast_result"):
            if str(row.get("指标标识") or "") != metric_code:
                continue
            model_ids.add(int(row.get("模型配置id", 0)))
            linked.append(row)

        batches: list[dict[str, Any]] = []
        for model_id in sorted(model_ids):
            for batch in store.rows("forecast_batch"):
                if int(batch.get("模型配置id", 0)) != model_id:
                    continue
                if batch.get("状态") != "已完成" or not batch.get("active"):
                    continue
                batch_rows = [row for row in linked if int(row.get("模型配置id", 0)) == model_id
                              and row.get("批次号") == batch.get("批次号")]
                window = int(batch.get("历史窗口", 0) or 0)
                meta = next((sample for sample in batch.get("样本") or []
                             if str(sample.get("指标标识", "")) == metric_code), None)
                if meta:
                    window = int(meta.get("历史窗口", window) or window)
                periods = sorted(int(row.get("周期序号", 0)) for row in batch_rows)
                expected_start = window + 1
                batch_issues: list[str] = []
                if declared_window and window > declared_window:
                    batch_issues.append(
                        f"{metric_code} 预测历史窗口 {window} 超过指标定义时间窗口 {declared_window}")
                if not periods or periods[0] != expected_start:
                    batch_issues.append(
                        f"{metric_code} 预测起点应为第{expected_start}周期，实际为第{periods[0] if periods else 0}周期")
                if periods and periods != list(range(expected_start, expected_start + len(periods))):
                    batch_issues.append(f"{metric_code} 预测周期不连续")
                issues.extend(batch_issues)
                batches.append({
                    "模型配置id": model_id,
                    "模型编号": batch.get("模型编号", ""),
                    "训练批次": batch.get("批次号", ""),
                    "历史窗口": window,
                    "预测周期": batch.get("预测周期", 0),
                    "预测起点": f"第{expected_start}周期",
                    "预测终点": f"第{expected_start + len(periods) - 1}周期" if periods else "",
                    "零斜率策略": batch.get("零斜率策略", ""),
                    "预测点": periods,
                    "校验通过": not batch_issues,
                    "问题": batch_issues,
                })
        return {
            "指标编号": metric_code,
            "指标名称": entry.get("指标名称", ""),
            "时间窗口": entry.get("时间窗口", ""),
            "留档版本": "V030",
            "联动表": "forecast_result",
            "批次": batches,
            "校验通过": not issues,
            "问题": issues,
            "items": [dict(row) for row in sorted(
                linked, key=lambda row: (int(row.get("模型配置id", 0)), int(row.get("周期序号", 0))))],
        }


def _parse_window(value: Any) -> int:
    try:
        window = int(str(value).strip())
    except Exception:
        return 0
    return window if window > 0 else 0
