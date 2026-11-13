"""接口出入参模型：列表分页、动作结果与各模块的明细结构。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    """登记或修改一条业务记录时提交的字段集合。"""

    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class VersionChangePayload(BaseModel):
    """换版请求：目标版本 + 期望的当前生效版本，用于并发冲突检测。"""

    target_version: str
    expected_version: str | None = None
    force_rerun: bool = False


class CleanruleChangePayload(BaseModel):
    """清洗规则换版请求：目标版本、期望版本和受影响区间。"""

    target_version: str
    expected_version: str | None = None
    affected_from: str | None = None
    affected_to: str | None = None



class LandingEntry(BaseModel):
    """落地批次明细结构。"""

    field_0: str | None = None  # 批次编号
    field_1: str | None = None  # 来源系统
    field_2: str | None = None  # 落地路径
    field_3: str | None = None  # 文件格式
    field_4: str | None = None  # 接收时间
    field_5: str | None = None  # 原始行数
    field_6: str | None = None  # 校验摘要
    field_7: str | None = None  # 批次状态

class OdsEntry(BaseModel):
    """贴源表明细结构。"""

    field_0: str | None = None  # 表编号
    field_1: str | None = None  # 表名称
    field_2: str | None = None  # 来源系统
    field_3: str | None = None  # 原始字段数
    field_4: str | None = None  # 增量字段
    field_5: str | None = None  # 全量快照策略
    field_6: str | None = None  # 每日增量行
    field_7: str | None = None  # 最近加载时间

class DwdEntry(BaseModel):
    """明细表明细结构。"""

    field_0: str | None = None  # 表编号
    field_1: str | None = None  # 表名称
    field_2: str | None = None  # 业务域
    field_3: str | None = None  # 主键列
    field_4: str | None = None  # 度量列
    field_5: str | None = None  # 时间分区列
    field_6: str | None = None  # 上游贴源表
    field_7: str | None = None  # 刷新水位

class DimEntry(BaseModel):
    """维度表明细结构。"""

    field_0: str | None = None  # 维度编号
    field_1: str | None = None  # 维度名称
    field_2: str | None = None  # 自然键列
    field_3: str | None = None  # 代理键列
    field_4: str | None = None  # 历史策略
    field_5: str | None = None  # 生效日期
    field_6: str | None = None  # 失效日期
    field_7: str | None = None  # 刷新周期

class DwsEntry(BaseModel):
    """汇总表明细结构。"""

    field_0: str | None = None  # 表编号
    field_1: str | None = None  # 表名称
    field_2: str | None = None  # 统计粒度
    field_3: str | None = None  # 汇总维度
    field_4: str | None = None  # 聚合度量
    field_5: str | None = None  # 时间窗口
    field_6: str | None = None  # 增量策略
    field_7: str | None = None  # 下游依赖

class AdsEntry(BaseModel):
    """应用表明细结构。"""

    field_0: str | None = None  # 表编号
    field_1: str | None = None  # 表名称
    field_2: str | None = None  # 业务场景
    field_3: str | None = None  # 来源汇总表
    field_4: str | None = None  # 指标列定义
    field_5: str | None = None  # 筛选条件
    field_6: str | None = None  # 刷新频率
    field_7: str | None = None  # 业务负责人

class PartitionEntry(BaseModel):
    """分区方案明细结构。"""

    field_0: str | None = None  # 方案编号
    field_1: str | None = None  # 数据表名
    field_2: str | None = None  # 分区字段
    field_3: str | None = None  # 分区粒度
    field_4: str | None = None  # 保留天数
    field_5: str | None = None  # 冷热阈值
    field_6: str | None = None  # 预建天数
    field_7: str | None = None  # 归档路径

class QualityEntry(BaseModel):
    """质量规则明细结构。"""

    field_0: str | None = None  # 规则编号
    field_1: str | None = None  # 校验表名
    field_2: str | None = None  # 目标字段
    field_3: str | None = None  # 校验表达式
    field_4: str | None = None  # 阈值下界
    field_5: str | None = None  # 阈值上界
    field_6: str | None = None  # 采样比例
    field_7: str | None = None  # 阻断级别

class LineageEntry(BaseModel):
    """血缘关系明细结构。"""

    field_0: str | None = None  # 关系编号
    field_1: str | None = None  # 上游表名
    field_2: str | None = None  # 下游表名
    field_3: str | None = None  # 上游字段
    field_4: str | None = None  # 下游字段
    field_5: str | None = None  # 转换说明
    field_6: str | None = None  # 采集批次
    field_7: str | None = None  # 关系版本

class BackfillEntry(BaseModel):
    """补数任务明细结构。"""

    field_0: str | None = None  # 任务编号
    field_1: str | None = None  # 目标表名
    field_2: str | None = None  # 开始日期
    field_3: str | None = None  # 结束日期
    field_4: str | None = None  # 并行度
    field_5: str | None = None  # 覆盖策略
    field_6: str | None = None  # 完成水位
    field_7: str | None = None  # 失败区间

class CleanruleEntry(BaseModel):
    """清洗规则明细结构。"""

    field_0: str | None = None  # 规则编号
    field_1: str | None = None  # 输入表名
    field_2: str | None = None  # 空值策略
    field_3: str | None = None  # 格式表达式
    field_4: str | None = None  # 异常阈值
    field_5: str | None = None  # 输出字段
    field_6: str | None = None  # 规则版本
    field_7: str | None = None  # 生效日期

class TransformEntry(BaseModel):
    """转换节点明细结构。"""

    field_0: str | None = None  # 节点编号
    field_1: str | None = None  # 输入表名
    field_2: str | None = None  # 输出表名
    field_3: str | None = None  # 转换逻辑
    field_4: str | None = None  # 执行顺序
    field_5: str | None = None  # 依赖节点
    field_6: str | None = None  # 失败策略
    field_7: str | None = None  # 调度周期

class DictionaryEntry(BaseModel):
    """字典条目明细结构。"""

    field_0: str | None = None  # 条目编号
    field_1: str | None = None  # 表名
    field_2: str | None = None  # 字段名称
    field_3: str | None = None  # 字段类型
    field_4: str | None = None  # 字段长度
    field_5: str | None = None  # 业务含义
    field_6: str | None = None  # 枚举说明
    field_7: str | None = None  # 字典版本

class MetamodelEntry(BaseModel):
    """元数据项明细结构。"""

    field_0: str | None = None  # 元数据编号
    field_1: str | None = None  # 对象类型
    field_2: str | None = None  # 对象名称
    field_3: str | None = None  # 属性名称
    field_4: str | None = None  # 属性值
    field_5: str | None = None  # 技术负责人
    field_6: str | None = None  # 业务负责人
    field_7: str | None = None  # 更新周期

class LifecycleEntry(BaseModel):
    """生命周期策略明细结构。"""

    field_0: str | None = None  # 策略编号
    field_1: str | None = None  # 数据表名
    field_2: str | None = None  # 保留天数
    field_3: str | None = None  # 冷数据阈值
    field_4: str | None = None  # 归档格式
    field_5: str | None = None  # 删除确认
    field_6: str | None = None  # 归档路径
    field_7: str | None = None  # 最后执行时间

class CostEntry(BaseModel):
    """成本账单明细结构。"""

    field_0: str | None = None  # 账单编号
    field_1: str | None = None  # 数据表名
    field_2: str | None = None  # 存储大小GB
    field_3: str | None = None  # 每日扫描量GB
    field_4: str | None = None  # 查询次数
    field_5: str | None = None  # 存储成本
    field_6: str | None = None  # 计算成本
    field_7: str | None = None  # 统计月份

class SyncEntry(BaseModel):
    """同步通道明细结构。"""

    field_0: str | None = None  # 通道编号
    field_1: str | None = None  # 来源表名
    field_2: str | None = None  # 目标表名
    field_3: str | None = None  # 同步方式
    field_4: str | None = None  # 延迟秒数
    field_5: str | None = None  # 并发度
    field_6: str | None = None  # 断点续传
    field_7: str | None = None  # 最后同步时间

class AuditlogEntry(BaseModel):
    """变更记录明细结构。"""

    field_0: str | None = None  # 记录编号
    field_1: str | None = None  # 操作表名
    field_2: str | None = None  # 变更类型
    field_3: str | None = None  # 变更前值
    field_4: str | None = None  # 变更后值
    field_5: str | None = None  # 操作人
    field_6: str | None = None  # 操作时间
    field_7: str | None = None  # 变更原因
