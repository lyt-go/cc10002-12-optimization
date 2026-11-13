# 数仓分层建模与治理平台

面向原始数据落地、清洗转换、分层建模、维度事实设计、分区治理、质量校验、血缘追踪与历史补数的数据仓库工程管理平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
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
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 质量换版判定链路

质量规则与清洗规则都按版本管理，换版链路遵循以下口径：

- **版本账本**：同一规则同一时刻只允许一个「已评审且已生效」版本；
  并发换版时调用方带 `expected_version`，冲突一律以持久化账本为准。
- **采样复用**：采样结论按校验表 + 目标字段 + 规则版本 + 采样比例生成
  `sample_key`，同一版本重复采样只生效一次；只收紧阈值时复用样本、
  只重评边界，不重新采样。
- **立即失效**：规则表达式、校验表/字段或阻断级别变化时旧结论立即失效，
  不允许把旧版本「通过」挂到新版本上。
- **阈值判定**：闭区间命中边界仍通过，开区间命中边界即越界；零样本、
  采样失败、异常中断和重试超时分别落到「待判定 / 采样失败 / 异常中断 /
  重试超时」，一律不沿用上次通过。
- **历史保留**：判定结果按版本保留，同版本重新判定只保留一条活跃结论，
  其余标记失效。
- **清洗联动**：清洗规则换版改变输出字段时，发布同批次标记受影响区间，
  只重跑区间内必要样本；新增输出字段没有历史样本时判为待判定。
- **两页联动**：清洗页的「质量结论 / 阻断结论 / 受影响区间」来自质量页
  同一份表级汇总（`GET /api/quality/table/{table}`），两页可互相跳转并
  共享 `table` 筛选。

示例：`QUAL_PAY_AMOUNT` 的 v2 采样 10% 得到异常率 2.4%（当时上界 3%，结论通过）；
v3 只把上界收紧到 2% 时，复用 v2 样本只重评边界，结论改为阻断，v2 的历史结论不变。

采样快照保留 `sample_key`、`sample_hash`、`sample_size`、`bad_rows`、
`bad_rate`、`boundary_value`、`lower_boundary_value`、`boundary_side`、
`boundary_hit`、`policy`、`evaluated_at`、`reuse_count` 与 `affected_range`。

| 接口 | 说明 |
| --- | --- |
| `GET /api/quality?table=&field=&version=&status=` | 质量规则列表（带版本、结论、快照摘要） |
| `GET /api/quality/rules/{规则编号}/versions` | 版本列表：评审状态、生效状态、最新结论 |
| `GET /api/quality/rules/{规则编号}?version=` | 采样快照与判定历史 |
| `POST /api/quality/{id}/change-version` | 换版重评（`target_version` + `expected_version`） |
| `POST /api/quality/{id}/sample` | 采样上报（含失败/中断/超时状态） |
| `GET /api/quality/table/{表名}` | 表级质量结论汇总（清洗页复用） |
| `GET /api/cleanrule/rules/{规则编号}/versions` | 清洗版本列表与受影响区间 |
| `POST /api/cleanrule/{id}/change-version` | 清洗换版并触发质量联动重评 |

测试：

```bash
cd backend
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 原始落地 | `landing` | 落地批次 | 批次编号、来源系统、落地路径 |
| 贴源层 | `ods` | 贴源表 | 表编号、表名称、来源系统 |
| 明细层 | `dwd` | 明细表 | 表编号、表名称、业务域 |
| 维度层 | `dim` | 维度表 | 维度编号、维度名称、自然键列 |
| 汇总层 | `dws` | 汇总表 | 表编号、表名称、统计粒度 |
| 应用层 | `ads` | 应用表 | 表编号、表名称、业务场景 |
| 分区治理 | `partition` | 分区方案 | 方案编号、数据表名、分区字段 |
| 质量校验 | `quality` | 质量规则 | 规则编号、校验表名、目标字段 |
| 数据血缘 | `lineage` | 血缘关系 | 关系编号、上游表名、下游表名 |
| 历史补数 | `backfill` | 补数任务 | 任务编号、目标表名、开始日期 |
| 清洗规则 | `cleanrule` | 清洗规则 | 规则编号、输入表名、空值策略 |
| 转换编排 | `transform` | 转换节点 | 节点编号、输入表名、输出表名 |
| 数据字典 | `dictionary` | 字典条目 | 条目编号、表名、字段名称 |
| 元数据管理 | `metamodel` | 元数据项 | 元数据编号、对象类型、对象名称 |
| 数据生命周期 | `lifecycle` | 生命周期策略 | 策略编号、数据表名、保留天数 |
| 存储成本 | `cost` | 成本账单 | 账单编号、数据表名、存储大小GB |
| 同步通道 | `sync` | 同步通道 | 通道编号、来源表名、目标表名 |
| 变更审计 | `auditlog` | 变更记录 | 记录编号、操作表名、变更类型 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数仓与数据湖
