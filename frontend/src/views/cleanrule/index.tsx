import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { request } from '@/api/client'

const ENDPOINT = '/api/cleanrule'
const columns = [
  "规则编号",
  "输入表名",
  "空值策略",
  "格式表达式",
  "异常阈值",
  "输出字段",
  "规则版本",
  "生效日期",
  "当前生效版本",
  "输出字段是否变化",
  "受影响区间",
  "质量结论",
  "阻断结论",
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
  输入表名?: string
  当前生效版本?: string | null
  版本是否已生效?: boolean
}

type VersionOption = {
  规则版本: string
  审核状态: string
  已评审: boolean
  已生效: boolean
  受影响区间: string | null
  输出字段: string[] | null
}

export default function CleanruleView() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [versions, setVersions] = useState<Record<string, VersionOption[]>>({})
  // 筛选键直接对应后端参数，避免把中文列名当查询参数发出去。
  const filterFields = useMemo(
    () => [
      { key: 'keyword', label: '规则编号/表名', placeholder: '按规则编号、输入表名或空值策略检索' },
      { key: 'table', label: '输入表名', placeholder: '按输入表名精确过滤' },
      { key: 'version', label: '规则版本', placeholder: '如 v2' },
    ],
    [],
  )

  // 从质量页跳转过来时带上 table 参数，两页共享同一个筛选口径。
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
        throw new Error('清洗规则列表读取失败')
      }
      const payload = await response.json()
      const items: Row[] = payload.items ?? []
      setRows(items)
      setTotal(payload.total ?? 0)

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
      setErrorMessage(error instanceof Error ? error.message : '清洗规则列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

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
        .catch(() => ({ ok: false, message: '清洗规则动作未生效，请稍后重试' }))
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '清洗规则动作未生效，请稍后重试')
      }
      setNotice(payload.message ?? '动作已生效')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '清洗规则操作失败')
    }
  }

  async function changeVersion(row: Row, target: string) {
    setErrorMessage('')
    setNotice('')
    try {
      // 换版与质量联动同一批次：受影响区间由后端按输出字段差异 + 生效日期推导。
      const response = await request(`${ENDPOINT}/${row.id}/change-version`, {
        method: 'POST',
        body: JSON.stringify({
          target_version: target,
          expected_version: String(row.当前生效版本 ?? '') || undefined,
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

  const stats = useMemo(
    () => [
      { label: '清洗规则总数', value: total },
      { label: '已启用规则', value: rows.filter((row) => row.版本是否已生效).length },
      { label: '输出字段变化', value: rows.filter((row) => row.输出字段是否变化).length },
      { label: '阻断结论', value: rows.filter((row) => row.阻断结论 === '阻断').length },
    ],
    [rows, total],
  )

  return (
    <section className="page" data-module="cleanrule">
      <header className="page-head">
        <div>
          <h2>清洗规则管理</h2>
          <p className="page-desc">
            清洗换版改变输出字段时标记受影响区间并只重跑必要样本，质量结论与质量页同源。
          </p>
        </div>
        <div className="page-actions">
          <button
            className="btn primary"
            type="button"
            onClick={() => setErrorMessage('清洗规则登记入口尚未接入审批流')}
          >
            登记清洗规则
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出清洗规则清单
          </button>
          {rows.length > 0 && (
            <Link
              className="btn ghost"
              to={`/quality?table=${encodeURIComponent(String(filters.table || rows[0].输入表名 || ''))}`}
            >
              查看质量结论
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
              onChange={(event) => {
                const value = event.target.value
                setFilters((prev) => ({ ...prev, [field.key]: value }))
                // 输入表名同步到地址栏，刷新后仍保持同一筛选口径。
                if (field.key === 'table') setSearchParams(value ? { table: value } : {})
              }}
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
        <button
          className="btn ghost"
          type="button"
          onClick={() => { setFilters({}); setSearchParams({}) }}
        >
          重置条件
        </button>
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
                  if (column === '输出字段是否变化') {
                    return <td key={column}>{row[column] ? '是' : '否'}</td>
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
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无清洗规则数据，可先登记清洗规则</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条清洗规则记录</span>
        {notice && <span className="notice-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
