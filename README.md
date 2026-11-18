# 多维分析实验平台

面向实验设计、指标计算、样本分析、效果评估与结论发布的数据分析实验平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 实验设计 | `experiment` | 实验方案 | 方案编号、实验名称、对照组说明 |
| 样本分群 | `sample_group` | 样本分群 | 分群编号、分群名称、筛选条件 |
| 指标定义 | `metric_def` | 指标定义 | 指标编号、指标名称、计算公式 |
| AB结果分析 | `ab_result` | 分析报告 | 报告编号、实验编号、分析指标 |
| 分群对比 | `segment_compare` | 对比任务 | 任务编号、分群A、分群B |
| 多维下钻 | `drill_down` | 下钻维度 | 维度编号、维度名称、下钻层级 |
| 数据筛选 | `data_filter` | 筛选规则 | 规则编号、数据来源、筛选字段 |
| 归因分析 | `attr_analysis` | 归因任务 | 任务编号、目标指标、候选因子 |
| 异常检测 | `anomaly_detect` | 检测规则 | 规则编号、监控指标、检测算法 |
| 预测模型 | `forecast_model` | 模型配置 / 训练批次 / 预测结果表 | 模型编号、预测对象、历史窗口 |

预测模型模块除了可见的 `forecast_model` 配置表，还有两张内部表：

- `forecast_batch`：训练批次，记录历史窗口、预测周期、零斜率策略、版本号与状态（已完成/已回滚）。
- `forecast_result`：预测结果表，每行对应一个指标的一个预测周期，带 `训练批次`、`预测周期配置`
  与 `版本号`，保证预测周期与训练批次成对落库。

内部表不计入运营概览的模块清单。预测相关接口：

| 接口 | 说明 |
| --- | --- |
| `POST /api/forecast_model/run` | 按模型配置运行训练，返回整份预测结果表 |
| `POST /api/forecast_model/{id}/train` | 指定历史观测与训练批次训练；`interrupt_after` 可复现中断 |
| `GET /api/forecast_model/results` | 接收方按 `模型编号` / `entry_id` / `批次号` 获取整份结果表 |
| `GET /api/forecast_model/{id}/validate` | 按历史窗口核对预测周期位置（首周期 = 历史窗口 + 1） |
| `POST /api/forecast_model/{id}/rollback` | 回滚到上一完整批次并清理残留，按 V030 留档 |
| `GET /api/forecast_model/{id}/archive` | V030 留档核对各批次完整性与回滚记录 |
| `GET /api/metric_def/{id}/forecast-results` | 指标定义按指标标识读取预测结果表并校验周期位置 |

预测规则：

- 线性回归的预测位置从历史末端之后开始（历史为 `0..n-1`，首个预测位置是 `n`），不重复末点。
- 零斜率批次统一采用一个策略：`mean` 取历史均值（结果表显示“常数均值预测”与零斜率说明），
  `last` 保留最近观测值（显示“最近值预测”与“零增长边界”）。同一批次所有样本共用一个版本号。
- 每个训练批次整体提交或整体回滚；中断时上一完整批次保持发布，半段结果不会出现在结果表里。
- 工作台、详情页、导出与指标定义联动都读同一张预测结果表，不重新计算，所以刷新前后一致。

| 数据导出 | `data_export` | 导出任务 | 任务编号、数据来源、导出格式 |
| 同期群追踪 | `cohort_track` | 同期群定义 | 群组编号、群组名称、首次行为 |
| 可视化配置 | `chart_config` | 图表配置 | 图表编号、图表类型、数据来源 |
| 洞察报告 | `insight_report` | 洞察报告 | 报告编号、分析主题、数据来源 |
| 数据新鲜度 | `data_freshness` | 新鲜度监控 | 监控编号、数据表名、最新分区 |
| 口径注册 | `metric_alias` | 口径版本 | 版本编号、指标编号、变更说明 |
| 实验因子 | `expr_factor` | 因子配置 | 因子编号、因子名称、因子层级 |
| 评估看板 | `evaluate_board` | 看板配置 | 看板编号、看板名称、关联实验 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用
