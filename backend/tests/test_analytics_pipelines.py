"""多维分析实验平台核心链路测试。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.store import store
from app.services.experiment import ExperimentService
from app.services.sample_group import SampleGroupService
from app.services.ab_result import AbResultService
from app.services.metric_def import MetricDefService
from app.services.expr_factor import ExprFactorService
from app.services.segment_compare import SegmentCompareService
from app.services.attr_analysis import AttrAnalysisService
from app.services.anomaly_detect import AnomalyDetectService
from app.services.forecast_model import ForecastModelService
from app.services.insight_report import InsightReportService
from app.services.metric_alias import MetricAliasService
from app.services.evaluate_board import EvaluateBoardService
from app.services.data_freshness import DataFreshnessService
from app.services.cohort_track import CohortTrackService
from app.services.data_export import DataExportService
from app.services.drill_down import DrillDownService

def _clear_all():
    for k in list(store._tables.keys()):
        store._tables[k] = []

def test_pipeline_experiment_to_sample_groups():
    """实验创建后自动生成对照和实验组分群。"""
    _clear_all()
    es = ExperimentService()
    entry, ms = es.create_entry({
        "方案编号": "EXPE-001",
        "实验名称": "新首页布局",
        "对照组说明": "旧版首页",
        "实验组说明": "新版首页",
        "样本总量": "10000",
        "分流比例": "5:5",
        "启动日期": "2026-11-01",
        "预期时长": "14",
    })
    assert entry is not None, ms
    sg_rows = store.rows("sample_group")
    assert len(sg_rows) >= 2, f"应创建对照组和实验组，实际{len(sg_rows)}"
    control = [r for r in sg_rows if r.get("group_type") == "control"]
    exp = [r for r in sg_rows if r.get("group_type") == "experiment"]
    assert len(control) >= 1
    assert len(exp) >= 1
    assert int(control[0].get("样本基数", 0)) > 0
    assert int(exp[0].get("样本基数", 0)) > 0

def test_sample_group_members():
    """分群能生成带差异的用户列表。"""
    _clear_all()
    # 先创建实验再用自动分群
    es = ExperimentService()
    es.create_entry({"方案编号":"EXPE-002","实验名称":"测试","对照组说明":"控","实验组说明":"试","样本总量":"200","分流比例":"5:5"})
    sg = SampleGroupService()
    members = sg.get_group_members(1)
    assert len(members) > 0, "应有分群用户"
    assert "user_id" in members[0]
    assert "metric_value" in members[0]

def test_metric_def_creation():
    """指标定义创建和口径版本。"""
    _clear_all()
    md = MetricDefService()
    entry, ms = md.create_entry({
        "指标编号": "M-001",
        "指标名称": "点击率",
        "计算公式": "clicks / impressions",
        "统计粒度": "日",
        "时间窗口": "7",
        "基准取值": "0.10",
        "精度位数": "4",
        "口径版本": "v1",
    })
    assert entry is not None, ms

def test_ab_result_analysis():
    """AB分析生成对照和实验组对比。"""
    _clear_all()
    ab = AbResultService()
    results = ab.run_analysis({"样本范围": "4"})
    assert len(results) == 4
    for r in results:
        assert "指标值" in r
        assert "对比基准" in r
        assert "差异" in r

def test_expr_factor_creation():
    """实验因子互斥规则。"""
    _clear_all()
    ef = ExprFactorService()
    entry, ms = ef.create_entry({
        "因子编号": "F-001",
        "因子名称": "按钮颜色",
        "因子层级": "UI层",
        "取值集合": "红色,蓝色",
        "分配比例": "5:5",
        "互斥规则": "颜色因子互斥",
        "关联实验": "EXPE-001",
        "生效批次": "B001",
    })
    assert entry is not None, ms

def test_anomaly_detect():
    """异常检测计算移动平均。"""
    _clear_all()
    ad = AnomalyDetectService()
    ad.create_entry({
        "规则编号": "AD-001",
        "监控指标": "点击率",
        "检测算法": "moving_average",
        "基线窗口": "7",
        "波动阈值": "0.3",
        "告警级别": "高",
        "静默时段": "0",
        "规则版本": "v1",
    })
    result = ad.run_detection(1, [0.10, 0.12, 0.11, 0.13, 0.15, 0.14, 0.55])
    assert result.get("ok") or "anomaly" in str(result).lower()

def test_forecast_model():
    """预测模型简单移动平均。"""
    _clear_all()
    fc = ForecastModelService()
    fc.create_entry({
        "模型编号": "FC-001",
        "预测对象": "点击率",
        "历史窗口": "7",
        "预测周期": "3",
        "模型类型": "moving_average",
        "参数配置": "window=3",
        "训练批次": "T001",
        "评估误差": "",
    })
    result = fc.train_and_predict(1, [0.10, 0.12, 0.11, 0.13, 0.15, 0.14, 0.16])
    assert result.get("ok"), result

def test_metric_alias_version():
    """口径版本注册。"""
    _clear_all()
    ma = MetricAliasService()
    entry, ms = ma.create_entry({
        "版本编号": "ALIAS-001",
        "指标编号": "M-001",
        "变更说明": "修改去重逻辑",
        "旧口径": "clicks/impressions",
        "新口径": "clicks/valid_impressions",
        "生效时间": "2026-11-16",
        "审批状态": "草稿",
        "变更人": "admin",
    })
    assert entry is not None, ms



# ---------------------------------------------------------------- 预测结果表 V030
def _forecast_fixture(model_type="linear_regression", periods="3", window="7",
                      config="", history="0.10,0.12,0.11,0.13,0.15,0.14,0.16"):
    _clear_all()
    fc = ForecastModelService()
    entry, missing = fc.create_entry({
        "模型编号": "FC-V030",
        "预测对象": "METR-V030",
        "历史窗口": window,
        "预测周期": periods,
        "模型类型": model_type,
        "参数配置": config,
        "训练批次": "",
        "评估误差": "",
        "历史观测": history,
    })
    assert entry is not None, missing
    return fc, entry


def test_linear_prediction_starts_after_history_end():
    """线性预测从历史末端之后的第一个周期起算，不重复历史最后一点。"""
    fc, entry = _forecast_fixture()
    history = [0.10, 0.12, 0.11, 0.13, 0.15, 0.14, 0.16]
    result = fc.train_and_predict(entry["id"], history)
    assert result["ok"], result
    periods = [row["周期序号"] for row in result["items"]]
    assert periods == [8, 9, 10], periods
    # 位置 7 才是历史最后一点；预测必须从位置 8 起，且严格递增
    n = len(history)
    x_mean = (n - 1) / 2
    y_mean = sum(history) / n
    slope = result["items"][0]["斜率"]
    intercept = y_mean - slope * x_mean
    expected = [round(slope * (n + i) + intercept, 6) for i in range(3)]
    assert result["predictions"] == expected, (result["predictions"], expected)
    assert result["predictions"][0] != history[-1]
    # 若沿用旧口径把末端位置 n-1 重复使用，首个预测值会等于末点的拟合值
    stale = round(slope * (n - 1) + intercept, 6)
    assert result["predictions"][0] != stale
    fetched = fc.get_batch_results(entry_id=entry["id"])
    assert fetched["预测起点"] == "第8周期" and fetched["预测终点"] == "第10周期"


def test_prediction_period_and_batch_persisted_together():
    """预测周期与训练批次必须成对落库：刷新后读到的仍是同一次训练的时间点。"""
    fc, entry = _forecast_fixture()
    result = fc.train_and_predict(entry["id"], [0.1, 0.2, 0.3, 0.4], training_batch="B-V030")
    assert result["ok"], result
    payload = fc.get_batch_results(entry_id=entry["id"])
    assert payload["训练批次"] == "B-V030"
    assert payload["预测周期"] == 3
    assert all(row["训练批次"] == "B-V030" for row in payload["items"])
    assert all(row["预测周期配置"] == 3 for row in payload["items"])
    assert all(row["预测周期"] == f"第{row['周期序号']}周期" for row in payload["items"])


def test_zero_slope_batch_uses_single_strategy():
    """零斜率与增长样本同批训练不回退版本，零斜率结果可解释且稳定。"""
    _clear_all()
    fc = ForecastModelService()
    entry, _ = fc.create_entry({
        "模型编号": "FC-MIX", "预测对象": "多指标", "历史窗口": "6",
        "预测周期": "2", "模型类型": "linear_regression", "参数配置": "", "训练批次": "",
    })
    samples = {"零增长指标": [0.5] * 6, "线性增长指标": [1, 2, 3, 4, 5, 6]}
    mean_result = fc.train_and_predict(entry["id"], samples, zero_slope_strategy="mean")
    assert mean_result["ok"], mean_result
    assert mean_result["predictions_by_metric"]["零增长指标"] == [0.5, 0.5]
    assert mean_result["predictions_by_metric"]["线性增长指标"] == [7.0, 8.0]
    zero_rows = [row for row in mean_result["items"] if row["指标标识"] == "零增长指标"]
    assert {row["预测类型"] for row in zero_rows} == {"常数均值预测"}
    assert {row["边界标记"] for row in zero_rows} == {"零斜率"}
    assert all("零斜率说明" in row["说明"] for row in zero_rows)
    # 同批次内所有行使用同一版本号，零斜率样本不会把增长样本覆盖掉
    assert len({row["版本号"] for row in mean_result["items"]}) == 1

    last_result = fc.train_and_predict(entry["id"], samples, zero_slope_strategy="last")
    assert last_result["ok"], last_result
    zero_rows = [row for row in last_result["items"] if row["指标标识"] == "零增长指标"]
    assert {row["预测类型"] for row in zero_rows} == {"最近值预测"}
    assert {row["边界标记"] for row in zero_rows} == {"零增长边界"}
    assert all(row["预测值"] == 0.5 for row in zero_rows)
    assert last_result["预测周期"] == 2


def test_interrupt_keeps_previous_complete_batch():
    """训练中断时保住上一完整批次，恢复过程不发布半段预测。"""
    fc, entry = _forecast_fixture()
    first = fc.train_and_predict(entry["id"], [0.1, 0.2, 0.3, 0.4, 0.5])
    assert first["ok"], first
    before = fc.get_batch_results(entry_id=entry["id"])
    interrupted = fc.train_and_predict(entry["id"], [0.2, 0.3, 0.4, 0.5, 0.6],
                                       training_batch="B-INTERRUPT", interrupt_after=1)
    assert not interrupted["ok"]
    after = fc.get_batch_results(entry_id=entry["id"])
    assert after["训练批次"] == before["训练批次"]
    assert after["items"] == before["items"]
    assert all(row["训练批次"] != "B-INTERRUPT" for row in store.rows("forecast_result"))
    resume = fc.train_and_predict(entry["id"], [0.2, 0.3, 0.4, 0.5, 0.6],
                                  training_batch="B-INTERRUPT")
    assert resume["ok"], resume
    assert len(resume["items"]) == 3


def test_result_dedupe_and_rollback_v030_archive():
    """重复入口只显示一次；回滚到上一完整批次并清残留，按 V030 留档。"""
    fc, entry = _forecast_fixture()
    fc.train_and_predict(entry["id"], [0.1, 0.2, 0.3, 0.4, 0.5], training_batch="B1")
    fc.train_and_predict(entry["id"], [0.2, 0.3, 0.4, 0.5, 0.6], training_batch="B2")
    # 模拟中断留下的半段批次与重复行
    store.rows("forecast_result").append({**store.rows("forecast_result")[0], "id": 999,
                                          "批次号": "GHOST", "周期序号": 99})
    duplicate = {**store.rows("forecast_result")[0], "id": 1000, "批次号": "B2"}
    store.rows("forecast_result").append(duplicate)
    active = fc.get_batch_results(entry_id=entry["id"])
    assert active["训练批次"] == "B2"
    assert len(active["items"]) == len({row["周期序号"] for row in active["items"]})
    assert active["total"] == 3

    rolled = fc.rollback(entry["id"])
    assert rolled["ok"], rolled
    assert rolled["训练批次"] == "B1"
    assert [row["周期序号"] for row in rolled["items"]] == [6, 7, 8]
    remaining = [row for row in store.rows("forecast_result")
                 if int(row["模型配置id"]) == entry["id"]]
    assert all(row["批次号"] == "B1" for row in remaining), remaining

    archive = fc.archive(entry["id"])
    assert archive["留档版本"] == "V030"
    records = {record["批次号"]: record for record in archive["items"]}
    assert records["B2"]["状态"] == "已回滚"
    assert records["B2"]["清理后剩余"] == 0
    assert records["B1"]["active"] is True


def test_metric_def_reads_forecast_results_v030():
    """指标定义读取预测结果表的指标标识，并按历史窗口校验预测周期位置。"""
    fc, entry = _forecast_fixture()
    fc.train_and_predict(entry["id"], [0.10, 0.12, 0.11, 0.13, 0.15, 0.14, 0.16])
    metric = MetricDefService()
    metric_entry, missing = metric.create_entry({
        "指标编号": "METR-V030", "指标名称": "预测指标", "计算公式": "sum(x)",
        "统计粒度": "日", "时间窗口": "7", "基准取值": "0.1", "精度位数": "4", "口径版本": "V030",
    })
    assert metric_entry is not None, missing
    linked = metric.get_forecast_results(metric_entry["id"])
    assert linked is not None
    assert linked["联动表"] == "forecast_result"
    assert linked["items"] and all(row["指标标识"] == "METR-V030" for row in linked["items"])
    assert linked["校验通过"], linked["问题"]
    batch = linked["批次"][0]
    assert batch["训练批次"] == linked["items"][0]["批次号"]
    assert batch["预测起点"] == "第8周期"

    # 指标定义的时间窗口比预测历史窗口还小时，周期位置校验应报出来
    metric_entry["时间窗口"] = "3"
    strict = metric.get_forecast_results(metric_entry["id"])
    assert not strict["校验通过"]
    assert any("超过指标定义时间窗口" in issue for issue in strict["问题"])
