"""预测模型 分析看板接口：模型配置、训练批次、预测结果表、回滚与留档。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.schemas import EntryPayload
from app.services.forecast_model import ForecastModelService

router = APIRouter(prefix="/api/forecast_model", tags=["预测模型"])
service = ForecastModelService()
LIST_FIELDS = ["模型编号", "预测对象", "历史窗口", "预测周期", "模型类型", "参数配置", "训练批次", "评估误差"]
RESULT_FIELDS = ["模型编号", "指标标识", "训练批次", "预测周期", "历史窗口", "周期序号",
                 "预测值", "预测类型", "零斜率", "边界标记", "说明"]
STATUSES = ["草稿", "训练中", "已就绪", "已废弃"]


class TrainingPayload(BaseModel):
    """训练入参：历史观测可以是数值列表、指标映射或多指标样本列表。"""

    values: Any = Field(default_factory=list)
    training_batch: str | None = None
    训练批次: str | None = None
    历史窗口: Any = None
    零斜率策略: str | None = None
    interrupt_after: int | None = None

    def history(self) -> Any:
        return self.values


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("/summary")
def get_summary() -> dict[str, Any]:
    """工作台摘要：取自已落库批次，刷新前后不漂移。"""
    return service.summary()


@router.get("")
def list_results() -> dict[str, Any]:
    """模型配置列表：每条带当前已发布批次的预测摘要。"""
    items = service.list_results()
    return {"items": items, "total": len(items)}


@router.post("/run")
def run_analysis(payload: EntryPayload) -> dict[str, Any]:
    """按参数运行一次训练，返回该批次的完整预测结果表。"""
    return service.run_analysis(payload.values)


@router.get("/results")
def get_results(
    模型编号: str | None = Query(default=None),
    entry_id: int | None = Query(default=None),
    批次号: str | None = Query(default=None),
) -> dict[str, Any]:
    """接收方按模型编号、配置 id 或批次号获取整份预测结果表。

    必须声明在 /{entry_id} 之前，否则会被当成路径参数。
    """
    return service.get_batch_results(entry_id=entry_id, 模型编号=模型编号, 批次号=批次号)


@router.get("/export")
def export_entries() -> StreamingResponse:
    """导出预测结果表（当前已发布批次），按模型与周期排序。"""
    items: list[dict[str, Any]] = []
    for entry in service.list_results():
        payload = service.get_batch_results(entry_id=int(entry["id"]))
        items.extend(payload.get("items", []))
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["id", *RESULT_FIELDS])
    for item in items:
        writer.writerow([csv_cell(item.get("id", "")),
                         *(csv_cell(item.get(field, "")) for field in RESULT_FIELDS)])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=forecast_model.csv"})


@router.get("/{entry_id}/results")
def get_entry_results(entry_id: int, 批次号: str | None = Query(default=None)) -> dict[str, Any]:
    payload = service.get_batch_results(entry_id=entry_id, 批次号=批次号)
    if not payload.get("ok") and payload.get("message") == "模型不存在":
        raise HTTPException(status_code=404, detail=f"模型配置 {entry_id} 不存在")
    return payload


@router.get("/{entry_id}/validate")
def validate_entry_results(entry_id: int, 批次号: str | None = Query(default=None)) -> dict[str, Any]:
    """按历史窗口核对预测周期位置，供跨模块与验收使用。"""
    payload = service.validate_periods(entry_id, 批次号)
    if not payload.get("ok") and payload.get("message") == "模型不存在":
        raise HTTPException(status_code=404, detail=f"模型配置 {entry_id} 不存在")
    return payload


@router.get("/{entry_id}/archive")
def archive_entry_results(entry_id: int) -> dict[str, Any]:
    """按 V030 留档核对每个训练批次的完整性与回滚记录。"""
    payload = service.archive(entry_id)
    if not payload.get("ok"):
        raise HTTPException(status_code=404, detail=f"模型配置 {entry_id} 不存在")
    return payload


@router.post("/{entry_id}/train")
def train_entry(entry_id: int, payload: TrainingPayload) -> dict[str, Any]:
    """训练一次并整批发布；中断时上一完整批次保持不变。"""
    return service.train_and_predict(
        entry_id,
        payload.history(),
        training_batch=payload.training_batch or payload.训练批次,
        zero_slope_strategy=payload.零斜率策略,
        history_window=payload.历史窗口,
        interrupt_after=payload.interrupt_after,
    )


@router.post("/{entry_id}/rollback")
def rollback_entry(entry_id: int, 批次号: str | None = Query(default=None)) -> dict[str, Any]:
    """回滚到上一完整批次并清理残留结果，按 V030 留档。"""
    return service.rollback(entry_id, 批次号)


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    """模型详情：与工作台展示同一份预测结果，不重新计算。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"模型配置 {entry_id} 不存在")
    detail = dict(entry)
    result = service.get_batch_results(entry_id=entry_id)
    detail["predictions"] = result.get("items", [])
    detail["result_summary"] = service._batch_summary(service._active_batch(entry_id))
    detail["预测起点"] = result.get("预测起点", "")
    detail["预测终点"] = result.get("预测终点", "")
    detail["结果状态"] = detail["result_summary"]["结果状态"]
    return detail
