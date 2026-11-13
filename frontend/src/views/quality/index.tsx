import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { request } from '@/api/client'

const ENDPOINT = '/api/quality'
const columns = [
  "规则编号",
  "校验表名",
  "目标字段",
  "校验表达式",
  "阈值下界",
  "阈值上界",
  "采样比例",
  "阻断级别",
  "规则版本",
  "当前生效版本",
  "判定状态",
  "质量结论",
  "阻断结论",
  "复用模式",
]
const actions = [
  "提交规则评审",
  "评审通过",
  "启用规则",
  "废弃规则",
]
const statuses = [
  "草稿",
  "规则评审中",
  "已启用",
  "已废弃",
]

type Row = Record<string, string | number | null> & {
  id: number
  规则编号?: string
  规则版本?: string
  校验表名?: string
  当前生效版本?: string | null
  版本是否已生效?: boolean
  采样快照?: Record<string, string | number | null> | null
}

type VersionOption = {
  规则版本: string
  审核状态: string
  已评审: boolean
  已生效: boolean
  结论: string | null
  阻断结论: string | null
  判定状态: string | null
  复用模式: string | null
  最近判定时间: string | null
}

type SampleSnapshot = {
  sample_key?: string
  sample_hash?: string
  sample_size?: number
  bad_rows?: number
  bad_rate?: number
  boundary_value?: number | null
  lower_boundary_value?: number | null
  boundary_side?: string | null
  boundary_hit?: boolean
  policy?: string
  evaluated_at?: string
  reuse_count?: number
  affected_range?: string | null
}

type Detail = {
  rule: string
  snapshots: SampleSnapshot[]
  history: Record<string, string | number | boolean | null>[]
}

export default function QualityView() {
  const [searchParams] = useSearchParams()
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [versions, setVersions] = useState<Record<string, VersionOption[]>>({})
  const [detail, setDetail] = useState<Detail | null>(null)
  // 筛选键直接对应后端参数，避免把中文列名当查询参数发出去。
  const filterFields = useMemo(
    () => [
      { key: 'keyword', label: '规则编号/字段', placeholder: '按规则编号、表名或字段检索' },
      { key: 'table', label: '校验表名', placeholder: '按校验表名精确过滤' },
      { key: 'field', label: '目标字段', placeholder: '按目标字段模糊过滤' },
      { key: 'version', label: '规则版本', placeholder: '如 v2' },
    ],
    [],
  )

  // 从清洗页跳转过来时带上 table 参数，两页共享同一个筛选口径。
  useEffect(() => {
    const table = searchParams.get('table')
    if (table) {
      setFilters((prev) => ({ ...prev, table }))
    }
  }, [searchParams])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('质量规则列表读取失败')
      }
      const payload = await response.json()
      const items: Row[] = payload.items ?? []
      setRows(items)
      setTotal(payload.total ?? 0)

      // 每个规则拉一次版本列表：版本、评审状态和结论都来自同一份账本。
      const ruleNos = Array.from(
        new Set(items.map((row) => row.规则编号).filter(Boolean)),
      ) as string[]
      const pairs = await Promise.all(
        ruleNos.map(async (ruleNo) => {
          const res = await request(`${ENDPOINT}/rules/${encodeURIComponent(ruleNo)}/versions`)
          if (!res.ok) return [ruleNo, []] as [string, VersionOption[]]
          const data = await res.json()
          return [ruleNo, data.items ?? []] as [string, VersionOption[]]
        }),
      )
      setVersions(Object.fromEntries(pairs))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '质量校验列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  const activeVersion = useCallback((row: Row): string => String(row.当前生效版本 ?? ''), [])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response
        .json()
        .catch(() => ({ ok: false, message: '质量校验动作未生效，请稍后重试' }))
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '质量校验动作未生效，请稍后重试')
      }
      setNotice(payload.message ?? '动作已生效')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '质量校验操作失败')
    }
  }

  async function changeVersion(row: Row, target: string) {
    setErrorMessage('')
    setNotice('')
    try {
      // expected_version 取持久化生效版本：并发换版时只有第一个请求能成功。
      const response = await request(`${ENDPOINT}/${row.id}/change-version`, {
        method: 'POST',
        body: JSON.stringify({
          target_version: target,
          expected_version: activeVersion(row) || undefined,
        }),
      })
      const payload = await response.json().catch(() => ({ ok: false, message: '换版未生效' }))
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '换版未生效，请稍后重试')
      }
      setNotice(payload.message ?? '换版已生效')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '换版失败')
    }
  }

  async function openDetail(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(
        `${ENDPOINT}/rules/${encodeURIComponent(String(row.规则编号))}?version=${encodeURIComponent(String(row.规则版本))}`,
      )
      if (!response.ok) {
        throw new Error('质量判定详情读取失败')
      }
      const payload = await response.json()
      setDetail({
        rule: `${row.规则编号} ${row.规则版本}`,
        snapshots: payload['采样快照'] ?? [],
        history: payload['判定历史'] ?? [],
      })
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '质量判定详情读取失败')
    }
  }

  const stats = useMemo(
    () => [
      { label: '质量规则总数', value: total },
      { label: '已启用规则', value: rows.filter((row) => row.版本是否已生效).length },
      { label: '阻断结论', value: rows.filter((row) => row.阻断结论 === '阻断').length },
      {
        label: '待判定',
        value: rows.filter((row) => row.判定状态 === '零样本' || row.判定状态 === '待判定').length,
      },
    ],
    [rows, total],
  )

  return (
    <section className="page" data-module="quality">
      <header className="page-head">
        <div>
          <h2>质量校验管理</h2>
          <p className="page-desc">
            按版本复用采样结论，阈值收紧后自动把通过改为阻断；历史结论按当时版本保留。
          </p>
        </div>
        <div className="page-actions">
          <button
            className="btn primary"
            type="button"
            onClick={() => setErrorMessage('质量规则登记入口尚未接入审批流')}
          >
            登记质量规则
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出质量校验清单
          </button>
          {rows.length > 0 && (
            <Link
              className="btn ghost"
              to={`/cleanrule?table=${encodeURIComponent(String(filters.table || rows[0].校验表名 || ''))}`}
            >
              查看对应清洗规则
            </Link>
          )}
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field.key} className="filter-item">
            <span>{field.label}</span>
            <input
              value={filters[field.key] ?? ''}
              placeholder={field.placeholder}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field.key]: event.target.value }))}
            />
          </label>
        ))}
        <label className="filter-item">
          <span>状态</span>
          <select
            value={filters.status ?? ''}
            onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}
          >
            <option value="">全部</option>
            {statuses.map((status) => <option key={status} value={status}>{status}</option>)}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const options = versions[String(row.规则编号)] ?? []
            const candidates = options.filter(
              (item) => item.已评审 && item.规则版本 !== row.当前生效版本,
            )
            return (
              <tr key={String(row.id)}>
                {columns.map((column) => {
                  if (column === '质量结论' || column === '阻断结论') {
                    const value = row[column] ?? '—'
                    const tone = row[column] === '阻断' ? 'block' : row[column] === '通过' ? 'pass' : 'pending'
                    return <td key={column}><span className={`badge ${tone}`}>{value}</span></td>
                  }
                  return <td key={column}>{row[column] ?? '—'}</td>
                })}
                <td className="row-actions">
                  {actions.map((action) => (
                    <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                      {action}
                    </button>
                  ))}
                  {candidates.map((candidate) => (
                    <button
                      key={`change-${candidate.规则版本}`}
                      className="link"
                      type="button"
                      onClick={() => void changeVersion(row, candidate.规则版本)}
                    >
                      换版到 {candidate.规则版本}
                    </button>
                  ))}
                  <button className="link" type="button" onClick={() => void openDetail(row)}>
                    采样详情
                  </button>
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无质量校验数据，可先登记质量规则</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <section className="detail-panel">
          <header className="detail-head">
            <h3>{detail.rule} 采样快照与判定历史</h3>
            <button className="link" type="button" onClick={() => setDetail(null)}>收起</button>
          </header>
          <table className="data-table">
            <thead>
              <tr>
                <th>sample_hash</th><th>sample_size</th><th>bad_rows</th><th>异常率</th>
                <th>boundary_value</th><th>下界</th><th>边界命中</th><th>区间策略</th>
                <th>复用次数</th><th>受影响区间</th><th>evaluated_at</th>
              </tr>
            </thead>
            <tbody>
              {detail.snapshots.length === 0 && (
                <tr><td className="empty-state" colSpan={11}>该版本暂无采样快照</td></tr>
              )}
              {detail.snapshots.map((item, index) => (
                <tr key={String(item.sample_key ?? index)}>
                  <td>{item.sample_hash ?? '—'}</td>
                  <td>{item.sample_size ?? '—'}</td>
                  <td>{item.bad_rows ?? '—'}</td>
                  <td>{item.bad_rate ?? '—'}%</td>
                  <td>{item.boundary_value ?? '—'}</td>
                  <td>{item.lower_boundary_value ?? '—'}</td>
                  <td>{item.boundary_hit ? `是（${item.boundary_side ?? '边界'}）` : '否'}</td>
                  <td>{item.policy ?? '—'}</td>
                  <td>{item.reuse_count ?? 0}</td>
                  <td>{item.affected_range ?? '—'}</td>
                  <td>{item.evaluated_at ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <table className="data-table">
            <thead>
              <tr>
                <th>批次</th><th>触发来源</th><th>状态</th><th>结论</th><th>阻断结论</th>
                <th>样本</th><th>异常行</th><th>异常率</th><th>复用模式</th><th>是否失效</th><th>判定时间</th>
              </tr>
            </thead>
            <tbody>
              {detail.history.map((item, index) => (
                <tr key={String(item['id'] ?? index)}>
                  <td>{item['批次'] ?? '—'}</td>
                  <td>{item['触发来源'] ?? '—'}</td>
                  <td>{item['state'] ?? '—'}</td>
                  <td>{item['conclusion'] ?? '—'}</td>
                  <td>{item['block_status'] ?? '—'}</td>
                  <td>{item['sample_size'] ?? '—'}</td>
                  <td>{item['bad_rows'] ?? '—'}</td>
                  <td>{item['bad_rate'] ?? '—'}%</td>
                  <td>{item['reuse_mode'] ?? '—'}</td>
                  <td>{item['invalidated'] ? '是' : '否'}</td>
                  <td>{item['evaluated_at'] ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条质量校验记录</span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
