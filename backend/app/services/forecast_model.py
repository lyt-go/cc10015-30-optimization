"""预测模型业务规则：读取模型配置与历史观测，按稳定批次生成预测结果表。

核心约定：
- 预测点从历史末端之后的第一个周期开始编号（历史最后位置是 n-1，首个预测位置是 n），
  不会再把历史最后一点重复当作下一周期。
- 每个训练批次要么整批落库、要么整批回滚；中断时保留上一完整批次的预测结果。
- 预测周期与训练批次在同一条预测结果记录里成对落库，刷新后不会指向不同时间点。
- 零斜率样本使用批次级统一策略（默认取历史均值，也可保留最近观测值），
  同一批次内不会因为零斜率样本与增长样本并存而互相覆盖版本。
"""
from __future__ import annotations

import math
import time
from typing import Any, Iterable

from app.store import store

MODULE = "forecast_model"
RESULT_TABLE = "forecast_result"
BATCH_TABLE = "forecast_batch"
REQUIRED_FIELDS = ["模型编号", "预测对象", "历史窗口"]
STATUS_ORDER = ["草稿", "训练中", "已就绪", "已废弃"]
ACTION_RULES = {"开始训练": "训练中", "确认就绪": "已就绪", "废弃模型": "已废弃"}
NEGATIVE_ACTIONS = []

# 零斜率样本没有趋势，直接外推只会得到“点不动”的重复值。
# 批次内统一采用一个策略，避免同批次里零斜率样本与增长样本互相覆盖版本。
ZERO_SLOPE_MEAN = "mean"
ZERO_SLOPE_LAST = "last"
ZERO_SLOPE_STRATEGIES = {
    ZERO_SLOPE_MEAN: "零斜率说明：历史序列无趋势，本批次零斜率预测统一取历史均值",
    ZERO_SLOPE_LAST: "零增长边界：历史序列无趋势，本批次零斜率预测保留最近观测值",
}
DEFAULT_ZERO_SLOPE = ZERO_SLOPE_MEAN
SLOPE_EPSILON = 1e-9


def _parse_periods(value: Any, default: int = 3) -> int:
    try:
        periods = int(str(value).strip())
    except Exception:
        return default
    return periods if periods > 0 else default


def _parse_window(value: Any, fallback: int) -> int:
    try:
        window = int(str(value).strip())
    except Exception:
        return fallback
    return window if window > 0 else fallback


def _parse_history(value: Any) -> list[float]:
    """历史观测可以是列表，也可以是逗号/空格/分号分隔的文本。"""
    if value is None:
        return []
    raw: Iterable[Any]
    if isinstance(value, (list, tuple)):
        raw = value
    else:
        text = str(value).replace("，", ",").replace(";", ",").replace("；", ",")
        text = text.replace(" ", ",").replace("\n", ",")
        raw = [part for part in text.split(",") if part.strip()]
    series: list[float] = []
    for item in raw:
        try:
            number = float(str(item).strip())
        except Exception:
            raise ValueError(f"历史观测包含无法解析的数值：{item}")
        if not math.isfinite(number):
            raise ValueError("历史观测必须是有限数值")
        series.append(number)
    return series


def _parse_zero_slope_strategy(value: Any, config: str = "") -> str:
    """显式入参优先，其次读参数配置，默认取历史均值。"""
    text = str(value or "").strip().lower()
    if text:
        if text in (ZERO_SLOPE_MEAN, "均值", "mean", "average", "常数均值"):
            return ZERO_SLOPE_MEAN
        if any(token in text for token in ("last", "recent", "最近", "保留", "零增长")):
            return ZERO_SLOPE_LAST
        return ZERO_SLOPE_MEAN
    config_text = str(config or "").lower()
    if any(token in config_text for token in ("last", "recent", "最近", "保留", "零增长")):
        return ZERO_SLOPE_LAST
    return ZERO_SLOPE_MEAN


def _linear_fit(values: list[float]) -> tuple[float, float]:
    n = len(values)
    x_mean = (n - 1) / 2
    y_mean = sum(values) / n
    numerator = sum((index - x_mean) * (values[index] - y_mean) for index in range(n))
    denominator = sum((index - x_mean) ** 2 for index in range(n))
    slope = numerator / denominator if denominator else 0.0
    intercept = y_mean - slope * x_mean
    return slope, intercept


class ForecastModelService:
    # ------------------------------------------------------------------ 配置
    def list_results(self) -> list[dict[str, Any]]:
        """模型配置工作台：每条配置带当前已发布批次的摘要，不做随机抖动。

        同一模型编号只保留 id 最大的一条，避免异常入口在列表里重复出现。
        """
        latest_by_code: dict[str, dict[str, Any]] = {}
        for row in store.rows(MODULE):
            key = str(row.get("模型编号") or f"id-{row.get('id')}")
            kept = latest_by_code.get(key)
            if kept is None or int(row.get("id", 0)) > int(kept.get("id", 0)):
                latest_by_code[key] = row
        results: list[dict[str, Any]] = []
        for row in sorted(latest_by_code.values(), key=lambda item: int(item.get("id", 0))):
            item = dict(row)
            batch = self._active_batch(int(row.get("id", 0)))
            item.update(self._batch_summary(batch))
            results.append(item)
        return results

    def _batch_summary(self, batch: dict[str, Any] | None) -> dict[str, Any]:
        if not batch:
            return {
                "最新批次": "", "结果状态": "未训练", "预测点数": 0,
                "预测起点": "", "预测终点": "", "零斜率策略": "", "批次版本": 0,
            }
        sample_meta = batch.get("样本") or [{
            "指标标识": batch.get("指标标识", ""),
            "历史窗口": batch.get("历史窗口", 0),
            "预测周期": batch.get("预测周期", 0),
        }]
        window = int(sample_meta[0].get("历史窗口", 0) or 0)
        periods = int(batch.get("预测周期", 0) or 0)
        expected = periods * max(1, len(sample_meta))
        rolled_back = batch.get("状态") == "已回滚"
        if rolled_back:
            # 回滚时已按 V030 清掉该批次残留，留档保留清理前的点数。
            published_points = int(batch.get("留档结果点数", 0) or 0)
        else:
            published_points = sum(
                1 for row in store.rows(RESULT_TABLE)
                if int(row.get("模型配置id", 0)) == int(batch.get("模型配置id", 0))
                and row.get("批次号") == batch.get("批次号") and row.get("active"))
        return {
            "最新批次": batch.get("批次号", ""),
            "结果状态": "已回滚" if rolled_back else "已发布",
            "预测点数": published_points,
            "预测起点": f"第{window + 1}周期" if window else "",
            "预测终点": f"第{window + periods}周期" if window and periods else "",
            "零斜率策略": batch.get("零斜率策略", ""),
            "批次版本": batch.get("版本号", 0),
            "训练批次": batch.get("批次号", ""),
            "历史窗口": window or batch.get("历史窗口", ""),
            "预测周期": periods or batch.get("预测周期", ""),
            "批次完整": published_points == expected,
        }

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def resolve_entry(self, *, entry_id: int | None = None,
                      模型编号: str | None = None) -> dict[str, Any] | None:
        if entry_id is not None:
            return self.get_entry(int(entry_id))
        if 模型编号:
            return store.find_by_field(MODULE, "模型编号", 模型编号)
        return None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [f for f in REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(r.get("id", 0)) for r in rows), default=0) + 1}
        for f in REQUIRED_FIELDS + ["预测周期", "模型类型", "参数配置", "训练批次", "评估误差", "历史观测"]:
            entry[f] = values.get(f, "")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    # ------------------------------------------------------------------ 批次
    def _batches(self, entry_id: int) -> list[dict[str, Any]]:
        return [batch for batch in store.rows(BATCH_TABLE)
                if int(batch.get("模型配置id", 0)) == entry_id]

    def _completed_batches(self, entry_id: int) -> list[dict[str, Any]]:
        return [batch for batch in self._batches(entry_id) if batch.get("状态") == "已完成"]

    def _active_batch(self, entry_id: int) -> dict[str, Any] | None:
        for batch in self._completed_batches(entry_id):
            if batch.get("active"):
                return batch
        return None

    def _batch_for_number(self, entry_id: int, batch_no: str) -> dict[str, Any] | None:
        for batch in self._batches(entry_id):
            if str(batch.get("批次号")) == str(batch_no):
                return batch
        return None

    def _next_batch_no(self, entry: dict[str, Any], requested: Any) -> str:
        code = str(entry.get("模型编号") or f"FCM-{entry['id']}")
        base = str(requested or "").strip() or str(entry.get("训练批次") or "").strip()
        if not base:
            base = f"{code}-B{len(self._completed_batches(int(entry['id']))) + 1:03d}"
        existing = {str(batch.get("批次号")) for batch in self._batches(int(entry["id"]))}
        if base in existing:
            suffix = 2
            while f"{base}-R{suffix}" in existing:
                suffix += 1
            base = f"{base}-R{suffix}"
        return base

    def _dedupe(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """同一指标同一周期只保留一条，重复入口不会再显示两遍。"""
        deduped: dict[tuple[str, int], dict[str, Any]] = {}
        for row in rows:
            key = (str(row.get("指标标识", "")), int(row.get("周期序号", 0)))
            kept = deduped.get(key)
            if kept is None or int(row.get("id", 0)) > int(kept.get("id", 0)):
                deduped[key] = row
        return [dict(deduped[key]) for key in sorted(deduped, key=lambda item: (item[0], item[1]))]

    def _active_rows(self, entry_id: int, batch_no: str | None = None) -> list[dict[str, Any]]:
        rows = [row for row in store.rows(RESULT_TABLE)
                if int(row.get("模型配置id", 0)) == entry_id and row.get("active")]
        if batch_no is not None:
            rows = [row for row in rows if row.get("批次号") == batch_no]
        return self._dedupe(rows)

    # ------------------------------------------------------------------ 样本与预测
    def _normalize_samples(self, entry: dict[str, Any], values: Any) -> list[dict[str, Any]]:
        """把入参规整成 [{指标标识, 历史观测, 历史窗口}]，支持单指标与多指标同批训练。"""
        default_name = str(entry.get("预测对象") or entry.get("模型编号") or "指标")
        samples: list[dict[str, Any]] = []
        if isinstance(values, dict):
            if "samples" in values or "样本" in values:
                return self._normalize_samples(entry, values.get("samples") or values.get("样本"))
            for name, history in values.items():
                samples.append({"指标标识": str(name), "历史观测": _parse_history(history)})
            return samples or [{"指标标识": default_name, "历史观测": []}]
        if isinstance(values, (list, tuple)) and values and isinstance(values[0], dict):
            for item in values:
                name = str(item.get("指标标识") or item.get("预测对象") or default_name)
                samples.append({
                    "指标标识": name,
                    "历史观测": _parse_history(item.get("历史观测", item.get("history", item.get("values")))),
                    "历史窗口": item.get("历史窗口", item.get("window")),
                })
            return samples
        samples.append({"指标标识": default_name, "历史观测": _parse_history(values)})
        return samples

    def _predict_sample(self, entry: dict[str, Any], sample: dict[str, Any], periods: int,
                        strategy: str) -> tuple[list[dict[str, Any]], str]:
        history = sample["历史观测"]
        model_type = str(entry.get("模型类型", "moving_average"))
        lowered = model_type.lower()
        n = len(history)
        rows: list[dict[str, Any]] = []
        note = ""

        if "linear" in lowered or "regression" in lowered:
            slope, intercept = _linear_fit(history)
            zero_slope = abs(slope) <= SLOPE_EPSILON
            if zero_slope:
                note = ZERO_SLOPE_STRATEGIES[strategy]
                if strategy == ZERO_SLOPE_LAST:
                    values = [history[-1] for _ in range(periods)]
                    label, boundary = "最近值预测", "零增长边界"
                else:
                    mean = sum(history) / n
                    values = [mean for _ in range(periods)]
                    label, boundary = "常数均值预测", "零斜率"
                for offset, value in enumerate(values, start=1):
                    rows.append(self._result_row(
                        entry, sample, n + offset, value, label, note,
                        zero_slope=True, boundary=boundary, slope=slope))
                return rows, note

            # 历史位置是 0..n-1，末端之后的第一个周期位置是 n，而不是重复 n-1。
            for offset in range(1, periods + 1):
                position = n - 1 + offset
                value = slope * position + intercept
                rows.append(self._result_row(
                    entry, sample, n + offset, value, "线性回归预测",
                    f"按历史末端之后的第{offset}个周期外推",
                    zero_slope=False, boundary="", slope=slope))
            return rows, "线性回归按历史末端之后的首个周期起算"

        if "moving" in lowered:
            try:
                window = int(str(entry.get("参数配置", "window=3")).replace("window=", "").strip())
            except Exception:
                window = 3
            window = max(1, window)
            extended = list(history)
            for offset in range(1, periods + 1):
                value = sum(extended[-window:]) / len(extended[-window:])
                extended.append(value)
                rows.append(self._result_row(
                    entry, sample, n + offset, value, "移动平均预测",
                    f"窗口={window}，逐步滚动生成", zero_slope=False, boundary="", slope=None))
            return rows, f"移动平均按窗口 {window} 滚动预测"

        mean = sum(history) / n
        for offset in range(1, periods + 1):
            rows.append(self._result_row(
                entry, sample, n + offset, mean, "均值预测", "模型类型未指定趋势，取历史均值",
                zero_slope=True, boundary="零斜率", slope=0.0))
        return rows, "模型类型未指定，按历史均值稳定输出"

    def _result_row(self, entry: dict[str, Any], sample: dict[str, Any], period_index: int,
                    value: float, label: str, note: str, *, zero_slope: bool,
                    boundary: str, slope: float | None) -> dict[str, Any]:
        return {
            "模型配置id": int(entry.get("id", 0)),
            "模型编号": entry.get("模型编号", ""),
            "指标标识": sample["指标标识"],
            "历史窗口": int(sample.get("实际窗口", 0) or 0),
            "周期序号": period_index,
            "预测周期": f"第{period_index}周期",
            "预测值": round(float(value), 6),
            "预测类型": label,
            "说明": note,
            "零斜率": zero_slope,
            "边界标记": boundary,
            "斜率": None if slope is None else round(float(slope), 8),
        }

    # ------------------------------------------------------------------ 训练
    def train_and_predict(self, entry_id: int, values: Any, *,
                          training_batch: Any = None,
                          zero_slope_strategy: Any = None,
                          history_window: Any = None,
                          interrupt_after: Any = None) -> dict[str, Any]:
        """执行一次训练批次并整批落库。

        任何一步失败都不会留下半段预测：预测结果表只会看到上一完整批次。
        """
        entry = self.get_entry(entry_id)
        if not entry:
            return {"ok": False, "message": "模型不存在"}

        try:
            samples = self._normalize_samples(entry, values)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        if not samples or any(len(sample["历史观测"]) < 2 for sample in samples):
            return {"ok": False, "message": "历史数据不足"}

        periods = _parse_periods(entry.get("预测周期"), 3)
        strategy = _parse_zero_slope_strategy(zero_slope_strategy, str(entry.get("参数配置", "")))
        batch_no = self._next_batch_no(entry, training_batch)
        if not batch_no or periods <= 0:
            return {"ok": False, "message": "预测周期与训练批次必须同时确定"}

        sample_meta: list[dict[str, Any]] = []
        result_rows: list[dict[str, Any]] = []
        notes: list[str] = []
        for sample in samples:
            history = sample["历史观测"]
            declared = sample.get("历史窗口")
            fallback_window = len(history)
            declared_window = _parse_window(
                declared if declared is not None else (
                    history_window if history_window is not None else entry.get("历史窗口")),
                fallback_window)
            # 配置声明的历史窗口优先；历史数据更长时只取末端窗口，保证周期位置可核对。
            effective_window = min(declared_window, len(history))
            if effective_window < 2:
                return {"ok": False, "message": "历史窗口不足，至少需要 2 个观测点"}
            sample["实际窗口"] = effective_window
            sample["历史观测"] = history[-effective_window:]
            sample_meta.append({
                "指标标识": sample["指标标识"],
                "历史窗口": effective_window,
                "预测周期": periods,
                "观测点数": len(history),
            })
            try:
                rows, note = self._predict_sample(entry, sample, periods, strategy)
            except Exception as exc:  # 计算失败：不触碰已发布批次
                return {"ok": False, "message": f"训练中断，已保留上一完整批次：{exc}"}
            if len(rows) != periods:
                return {"ok": False, "message": "预测结果不完整，已放弃本次批次"}
            result_rows.extend(rows)
            if note:
                notes.append(note)

        now = time.time()
        version = store.version(RESULT_TABLE) + 1
        aggregate_window = sample_meta[0]["历史窗口"] if len(sample_meta) == 1 else min(
            meta["历史窗口"] for meta in sample_meta)
        batch_record = {
            "id": 0,
            "批次号": batch_no,
            "模型配置id": int(entry["id"]),
            "模型编号": entry.get("模型编号", ""),
            "指标标识": sample_meta[0]["指标标识"] if len(sample_meta) == 1 else "多指标",
            "历史窗口": aggregate_window,
            "预测周期": periods,
            "零斜率策略": strategy,
            "状态": "已完成",
            "active": True,
            "版本号": version,
            "样本": sample_meta,
            "说明": "；".join(dict.fromkeys(notes)),
            "created_at": now,
        }
        for row in result_rows:
            row.update({
                "id": 0,  # 事务内按顺序分配，失败时不留下空洞 id
                "批次号": batch_no,
                "训练批次": batch_no,
                "预测周期配置": periods,
                "零斜率策略": strategy,
                "版本号": version,
                "状态": "已完成",
                "active": True,
                "created_at": now,
            })

        limit = None
        if interrupt_after is not None:
            try:
                limit = max(1, int(interrupt_after))
            except Exception:
                limit = 1

        try:
            with store.transaction(RESULT_TABLE, BATCH_TABLE):
                # 先把上一批次置为非活动，再整批写入新结果；事务失败两者一起恢复。
                for batch in store.rows(BATCH_TABLE):
                    if int(batch.get("模型配置id", 0)) == int(entry["id"]):
                        batch["active"] = False
                for row in store.rows(RESULT_TABLE):
                    if int(row.get("模型配置id", 0)) == int(entry["id"]):
                        row["active"] = False
                # 清理上一轮中断留下的半段数据：没有对应已完成批次的结果行。
                completed = {str(batch["批次号"]) for batch in self._completed_batches(int(entry["id"]))}
                store.replace_rows(RESULT_TABLE, [
                    row for row in store.rows(RESULT_TABLE)
                    if not (int(row.get("模型配置id", 0)) == int(entry["id"])
                            and str(row.get("批次号")) not in completed)
                ])
                next_id = store.next_id(RESULT_TABLE)
                batch_record["id"] = store.next_id(BATCH_TABLE)
                store.rows(BATCH_TABLE).append(batch_record)
                for offset, row in enumerate(result_rows):
                    if limit is not None and offset >= limit:
                        raise RuntimeError("模拟训练中断")
                    row["id"] = next_id + offset
                    store.rows(RESULT_TABLE).append(row)
        except Exception as exc:
            return {"ok": False, "message": f"预测结果表事务异常，已回滚并保留上一完整批次：{exc}"}

        entry["训练批次"] = batch_no
        entry["预测周期"] = str(periods)
        entry["历史窗口"] = str(aggregate_window)
        entry["status"] = "已就绪"
        entry["pending"] = False
        entry["abnormal"] = False

        return {
            "ok": True,
            "message": f"预测完成，{len(result_rows)}个周期",
            "predictions": [row["预测值"] for row in result_rows],
            "method": entry.get("模型类型", ""),
            "预测周期": periods,
            "训练批次": batch_no,
            "历史窗口": aggregate_window,
            "零斜率策略": strategy,
            "版本号": version,
            "说明": batch_record["说明"],
            "batch": batch_record,
            "items": result_rows,
            "predictions_by_metric": {
                meta["指标标识"]: [row["预测值"] for row in result_rows
                                    if row["指标标识"] == meta["指标标识"]]
                for meta in sample_meta
            },
        }

    # ------------------------------------------------------------------ 读取
    def get_batch_results(self, *, entry_id: int | None = None,
                          模型编号: str | None = None,
                          批次号: str | None = None) -> dict[str, Any]:
        """接收方获取某一批次的完整预测结果表；不传批次则取当前已发布批次。"""
        entry = self.resolve_entry(entry_id=entry_id, 模型编号=模型编号)
        if entry is None:
            return {"ok": False, "message": "模型不存在", "items": []}
        entry_id = int(entry["id"])
        batch: dict[str, Any] | None
        if 批次号:
            batch = self._batch_for_number(entry_id, 批次号)
            if batch is None or batch.get("状态") not in ("已完成", "已回滚"):
                return {"ok": False, "message": f"批次 {批次号} 不是完整批次", "items": []}
        else:
            batch = self._active_batch(entry_id)
            if batch is None:
                return {"ok": True, "message": "该模型还没有已发布的预测结果",
                        "items": [], "total": 0}
        rows = [row for row in store.rows(RESULT_TABLE)
                if int(row.get("模型配置id", 0)) == entry_id and row.get("批次号") == batch["批次号"]]
        if 批次号 and batch.get("active"):
            rows = [row for row in rows if row.get("active")]
        rows = self._dedupe(rows)
        window = int(batch.get("历史窗口", 0) or 0)
        periods = int(batch.get("预测周期", 0) or 0)
        samples = batch.get("样本") or []
        return {
            "ok": True,
            "模型配置id": entry_id,
            "模型编号": entry.get("模型编号", ""),
            "指标标识": batch.get("指标标识", entry.get("预测对象", "")),
            "训练批次": batch["批次号"],
            "预测周期": periods,
            "历史窗口": window,
            "零斜率策略": batch.get("零斜率策略", ""),
            "版本号": batch.get("版本号", 0),
            "status": batch.get("状态", ""),
            "说明": batch.get("说明", ""),
            "样本": samples,
            # 周期位置必须从历史末端之后开始：首个周期 = 历史窗口 + 1
            "预测起点": f"第{window + 1}周期" if window else "",
            "预测终点": f"第{window + periods}周期" if window and periods else "",
            "items": rows,
            "total": len(rows),
        }

    def latest_results(self, entry_id: int) -> dict[str, Any]:
        return self.get_batch_results(entry_id=entry_id)

    def validate_periods(self, entry_id: int, 批次号: str | None = None) -> dict[str, Any]:
        """按历史窗口核对预测周期位置：首个预测周期必须是历史窗口 + 1。"""
        payload = self.get_batch_results(entry_id=entry_id, 批次号=批次号)
        if not payload.get("ok"):
            return payload
        window_default = int(payload.get("历史窗口", 0) or 0)
        samples = {meta["指标标识"]: int(meta.get("历史窗口", window_default) or window_default)
                   for meta in payload.get("样本") or []}
        issues: list[str] = []
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in payload["items"]:
            groups.setdefault(str(row.get("指标标识", "")), []).append(row)
        for metric, rows in groups.items():
            window = samples.get(metric, int(rows[0].get("历史窗口", window_default) or window_default))
            expected_start = window + 1
            periods = sorted(int(row.get("周期序号", 0)) for row in rows)
            if not periods or periods[0] != expected_start:
                issues.append(f"{metric} 预测起点应为第{expected_start}周期，实际为第{periods[0] if periods else 0}周期")
            if periods != list(range(expected_start, expected_start + len(periods))):
                issues.append(f"{metric} 预测周期不连续")
            configured = int(payload.get("预测周期", 0) or 0)
            if configured and len(periods) != configured:
                issues.append(f"{metric} 预测点数应为{configured}，实际为{len(periods)}")
            if any(int(row.get("历史窗口", window) or window) != window for row in rows):
                issues.append(f"{metric} 同批次内历史窗口不一致")
        return {
            "ok": True,
            "模型配置id": payload["模型配置id"],
            "模型编号": payload["模型编号"],
            "训练批次": payload["训练批次"],
            "预测起点": payload.get("预测起点", ""),
            "预测终点": payload.get("预测终点", ""),
            "通过": not issues,
            "问题": issues,
            "items": payload["items"],
        }

    # ------------------------------------------------------------------ 回滚与留档
    def rollback(self, entry_id: int, 批次号: str | None = None) -> dict[str, Any]:
        """回滚当前已发布批次到上一完整批次，并清掉中断留下的残留行。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return {"ok": False, "message": "模型不存在", "items": []}
        completed = [batch for batch in self._batches(entry_id) if batch.get("状态") == "已完成"]
        active = next((batch for batch in completed if batch.get("active")), None)
        if active is None:
            return {"ok": False, "message": "没有可回滚的已发布批次", "items": []}
        if 批次号 and str(批次号) != str(active["批次号"]):
            return {"ok": False, "message": f"批次 {批次号} 不是当前已发布批次", "items": []}
        candidates = [batch for batch in completed if batch is not active]
        target = candidates[-1] if candidates else None
        if target is None:
            return {"ok": False, "message": "没有可恢复的上一完整批次", "items": []}
        try:
            with store.transaction(RESULT_TABLE, BATCH_TABLE):
                # 先按 V030 留档回滚批次的清理前点数，再整表清掉该批次的残留行。
                archived = [row for row in store.rows(RESULT_TABLE)
                            if int(row.get("模型配置id", 0)) == entry_id
                            and str(row.get("批次号")) == str(active["批次号"])]
                active["留档结果点数"] = len(archived)
                active["留档版本"] = "V030"
                valid = {str(batch["批次号"]) for batch in completed}
                store.replace_rows(RESULT_TABLE, [
                    row for row in store.rows(RESULT_TABLE)
                    if not (int(row.get("模型配置id", 0)) == entry_id
                            and (str(row.get("批次号")) not in valid
                                 or str(row.get("批次号")) == str(active["批次号"])))
                ])
                for row in store.rows(RESULT_TABLE):
                    if int(row.get("模型配置id", 0)) == entry_id:
                        row["active"] = str(row.get("批次号")) == str(target["批次号"])
                active["active"] = False
                active["状态"] = "已回滚"
                active["rollback_at"] = time.time()
                active["回滚原因"] = "预测结果表事务异常，按 V030 留档回滚"
                target["active"] = True
        except Exception as exc:
            return {"ok": False, "message": f"回滚失败，已保留原结果：{exc}", "items": []}
        result = self.get_batch_results(entry_id=entry_id)
        result["message"] = f"已回滚到批次 {target['批次号']}，残留结果已清理"
        result["回滚批次"] = active["批次号"]
        return result

    def archive(self, entry_id: int) -> dict[str, Any]:
        """按 V030 留档：列出该模型每个批次的状态、版本与结果点数，供核对。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return {"ok": False, "message": "模型不存在", "items": []}
        records: list[dict[str, Any]] = []
        for batch in self._batches(entry_id):
            rows = [row for row in store.rows(RESULT_TABLE)
                    if int(row.get("模型配置id", 0)) == entry_id and row.get("批次号") == batch["批次号"]]
            expected = int(batch.get("预测周期", 0) or 0) * max(1, len(batch.get("样本") or [1]))
            archived_points = batch.get("留档结果点数")
            records.append({
                "批次号": batch["批次号"],
                "状态": batch.get("状态", ""),
                "active": bool(batch.get("active")),
                "版本号": batch.get("版本号", 0),
                "历史窗口": batch.get("历史窗口", 0),
                "预测周期": batch.get("预测周期", 0),
                "零斜率策略": batch.get("零斜率策略", ""),
                "结果点数": len(rows) if archived_points is None else archived_points,
                "清理后剩余": len(rows),
                "完整": (len(rows) if archived_points is None else archived_points) == expected,
                "说明": batch.get("说明", ""),
                "回滚原因": batch.get("回滚原因", ""),
                "留档版本": batch.get("留档版本", ""),
            })
        records.sort(key=lambda item: int(item["版本号"]))
        return {"ok": True, "模型配置id": entry_id, "模型编号": entry.get("模型编号", ""),
                "留档版本": "V030", "items": records}

    def summary(self) -> dict[str, Any]:
        """工作台摘要：全部取自已落库的批次和结果，刷新前后一致。"""
        entries = store.rows(MODULE)
        published = [batch for batch in store.rows(BATCH_TABLE)
                     if batch.get("状态") == "已完成" and batch.get("active")]
        points = sum(1 for row in store.rows(RESULT_TABLE) if row.get("active"))
        return {"metrics": [
            {"label": "模型配置数", "value": len(entries), "trend": 0},
            {"label": "已发布批次", "value": len(published), "trend": 0},
            {"label": "预测点总数", "value": points, "trend": 0},
        ]}

    def run_analysis(self, params: dict[str, Any]) -> dict[str, Any]:
        """运行分析：按模型编号或配置 id 触发一次训练并返回结果表。

        这是页面「运行分析」入口，也是没有显式历史观测时的兜底训练入口。
        """
        model_code = str(params.get("模型编号") or "").strip()
        entry_id = params.get("模型配置id") or params.get("id")
        if model_code:
            entry = self.resolve_entry(模型编号=model_code)
        elif entry_id not in (None, ""):
            try:
                entry = self.get_entry(int(entry_id))
            except Exception:
                entry = None
        else:
            visible = self.list_results()
            entry = store.find(MODULE, int(visible[0]["id"])) if visible else None
        if entry is None:
            return {"ok": False, "message": "模型不存在", "items": []}
        history = params.get("历史观测", params.get("history"))
        if not history:
            history = entry.get("历史观测")
        if not history:
            return {"ok": False, "message": "缺少历史观测数据", "items": []}
        result = self.train_and_predict(
            int(entry["id"]), history,
            training_batch=params.get("训练批次"),
            zero_slope_strategy=params.get("零斜率策略"),
            history_window=params.get("历史窗口"),
        )
        if result.get("ok"):
            result.update(self.get_batch_results(entry_id=int(entry["id"])))
            result["ok"] = True
        return result
