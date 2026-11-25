const state = { markets: [], entities: [], configurations: [], jobs: [], entries: [], trend: [], expandedJobs: new Set(), page: 1, pageSize: 25, total: 0, snapshotSignature: '', pollTimer: null, playlistReference: '', historyPage: 1, historyPageSize: 50, historyTotal: 0 }
const byId = (id) => document.getElementById(id)
const toast = byId('toast')
let toastTimer

function escapeHtml(value = '') {
  return String(value).replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character])
}

function showToast(message) {
  toast.textContent = message
  toast.classList.add('show')
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => toast.classList.remove('show'), 3200)
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    credentials: 'same-origin',
    headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.detail || body.message || 'Something went wrong. Please try again.')
  return body
}

function localDate(date) {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function lastChartFriday() {
  const value = new Date()
  const daysSinceFriday = (value.getDay() + 2) % 7
  value.setDate(value.getDate() - daysSinceFriday)
  return localDate(value)
}

function setFormDates() {
  const latest = lastChartFriday()
  byId('start-date').value = latest
  byId('end-date').value = latest
  byId('filter-end').value = latest
  const start = new Date(`${latest}T12:00:00`)
  start.setDate(start.getDate() - 84)
  byId('filter-start').value = localDate(start)
}

function renderMarkets() {
  const allMarkets = state.markets
  byId('market-options').innerHTML = allMarkets.map((market) => `
    <label class="market-option" title="${escapeHtml(market.name)}">
      <input type="checkbox" name="market" value="${escapeHtml(market.code)}" ${market.code === 'US' ? 'checked' : ''} />
      <span>${escapeHtml(market.code === 'global' ? 'Global' : market.code)}</span>
    </label>`).join('')
  const marketOptions = allMarkets.map((market) => `<option value="${escapeHtml(market.code)}">${escapeHtml(market.name)}</option>`).join('')
  byId('filter-market').insertAdjacentHTML('beforeend', marketOptions)
  byId('live-market').innerHTML = marketOptions
  byId('live-market').value = 'US'
  byId('market-options').addEventListener('change', (event) => {
    const changed = event.target
    if (changed.value === 'global' && changed.checked) {
      byId('market-options').querySelectorAll('input').forEach((input) => { if (input !== changed) input.checked = false })
    } else if (changed.checked) {
      const global = byId('market-options').querySelector('input[value="global"]')
      if (global) global.checked = false
    }
    updateMarketCount()
  })
  updateMarketCount()
}

function selectedMarkets() {
  return [...byId('market-options').querySelectorAll('input:checked')].map((input) => input.value)
}

function updateMarketCount() {
  const count = selectedMarkets().length
  byId('market-count').textContent = `${count} selected`
  byId('start-job-button').disabled = count === 0
}

function renderEntities() {
  const select = byId('filter-entity')
  select.innerHTML = '<option value="">All entities</option>' + state.entities.map((entity, index) => {
    const kind = entity.entity_type === 'all' ? 'Whole chart' : entity.entity_type
    return `<option value="${index}">${escapeHtml(kind)} · ${escapeHtml(entity.entity_id)}</option>`
  }).join('')
}

async function loadEntities() {
  state.entities = await request('/api/entities')
  renderEntities()
}

function renderSetups() {
  const container = byId('setup-list')
  if (!state.configurations.length) {
    container.innerHTML = '<span class="muted">No saved setups yet.</span>'
    return
  }
  container.innerHTML = state.configurations.map((config) => `<button class="setup-chip" type="button" data-setup="${escapeHtml(config.id)}">${escapeHtml(config.name)}</button>`).join('')
  container.querySelectorAll('[data-setup]').forEach((button) => button.addEventListener('click', () => applyConfiguration(button.dataset.setup)))
}

function applyConfiguration(id) {
  const config = state.configurations.find((item) => item.id === id)
  if (!config) return
  byId('chart-type').value = config.chart_type
  byId('entity-type').value = config.entity_type
  byId('entity-id').value = config.entity_type === 'all' ? '' : config.entity_id
  byId('entity-field').hidden = config.entity_type === 'all'
  byId('configuration-name').value = config.name
  byId('market-options').querySelectorAll('input').forEach((input) => { input.checked = config.markets.includes(input.value) })
  updateMarketCount()
  showToast(`Loaded “${config.name}”.`)
}

async function loadConfigurations() {
  state.configurations = await request('/api/configurations')
  renderSetups()
}

function formatDate(value, options = { month: 'short', day: 'numeric', year: 'numeric' }) {
  if (!value) return '—'
  const date = new Date(`${value}T12:00:00`)
  return Number.isNaN(date.getTime()) ? '—' : new Intl.DateTimeFormat(undefined, options).format(date)
}

function formatCompact(value, suffix = '') {
  if (value == null || value === '' || !Number.isFinite(Number(value))) return '—'
  return `${new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(Number(value))}${suffix}`
}

function chartLabel(type) {
  return ({ TopSongs: 'Top songs', TopMusicVideos: 'Top music videos', TopArtists: 'Top artists' })[type] || type
}

function statusLabel(status) {
  return ({ queued: 'Queued', running: 'Collecting', paused: 'Paused', completed: 'Complete', partial: 'Partial', failed: 'Failed', pending: 'Pending', succeeded: 'Saved' })[status] || status
}

function renderJob(job) {
  const finished = Number(job.succeeded_tasks || 0) + Number(job.failed_tasks || 0)
  const total = Number(job.total_tasks || 0)
  const percent = total ? Math.round(finished * 100 / total) : 0
  const dateLabel = job.start_date === job.end_date ? formatDate(job.start_date) : `${formatDate(job.start_date)} – ${formatDate(job.end_date)}`
  const actions = []
  if (['queued', 'running'].includes(job.status)) actions.push(`<button class="button" data-job-action="pause" data-job-id="${escapeHtml(job.id)}" type="button">Pause</button>`)
  if (job.status === 'paused') actions.push(`<button class="button" data-job-action="resume" data-job-id="${escapeHtml(job.id)}" type="button">Resume</button>`)
  if (Number(job.failed_tasks) > 0) actions.push(`<button class="button" data-job-action="retry" data-job-id="${escapeHtml(job.id)}" type="button">Retry failed (${Number(job.failed_tasks)})</button>`)
  if ((job.markets || []).length) actions.push(`<button class="button" data-job-action="toggle-markets" data-job-id="${escapeHtml(job.id)}" type="button">Retry selected markets</button>`)
  actions.push(`<button class="button" data-job-action="tasks" data-job-id="${escapeHtml(job.id)}" type="button">${state.expandedJobs.has(job.id) ? 'Hide' : 'View'} task status</button>`)
  const retryPicker = `<div class="retry-picker" data-retry-picker hidden><fieldset><legend>Choose markets to recollect</legend>${(job.markets || []).map((market) => `<label><input type="checkbox" value="${escapeHtml(market)}"><span>${escapeHtml(market)}</span></label>`).join('')}</fieldset><button class="button button-accent" data-job-action="retry-selected" data-job-id="${escapeHtml(job.id)}" type="button">Recollect selected markets</button></div>`
  return `<article class="job-card" data-job-card="${escapeHtml(job.id)}">
    <div class="job-top"><div><div class="job-name">${escapeHtml(job.configuration_name)} · ${escapeHtml(chartLabel(job.chart_type))}</div><div class="job-meta">${escapeHtml(dateLabel)} &nbsp;·&nbsp; ${escapeHtml((job.markets || []).join(', '))} &nbsp;·&nbsp; ${total} daily tasks</div></div><span class="job-status ${escapeHtml(job.status)}">${escapeHtml(statusLabel(job.status))}</span></div>
    <div class="job-progress" role="progressbar" aria-label="Collection progress" aria-valuemin="0" aria-valuemax="${total}" aria-valuenow="${finished}"><span style="width:${percent}%"></span></div>
    <div class="job-counts"><span>${finished} / ${total} complete</span><span>${Number(job.succeeded_tasks || 0)} saved · ${Number(job.failed_tasks || 0)} failed · ${Number(job.remaining_tasks || 0)} pending</span></div>
    <div class="job-actions">${actions.join('')}</div>
    ${retryPicker}
    <div class="task-list" data-task-list="${escapeHtml(job.id)}" ${state.expandedJobs.has(job.id) ? '' : 'hidden'}></div>
  </article>`
}

function renderJobs() {
  const container = byId('jobs-list')
  if (!state.jobs.length) {
    container.innerHTML = '<div class="empty-panel">The shared collection queue is ready.<span>Start a job to see market-by-market progress here.</span></div>'
    return
  }
  container.innerHTML = state.jobs.map(renderJob).join('')
  container.querySelectorAll('[data-job-action]').forEach((button) => button.addEventListener('click', () => handleJobAction(button.dataset.jobId, button.dataset.jobAction)))
  state.jobs.filter((job) => state.expandedJobs.has(job.id)).forEach((job) => loadJobTasks(job.id, 1))
}

async function loadJobTasks(id, page = 1, append = false) {
  try {
    const job = await request(`/api/jobs/${encodeURIComponent(id)}?page=${page}&page_size=50`)
    const target = document.querySelector(`[data-task-list="${CSS.escape(id)}"]`)
    if (!target) return
    target.querySelector('[data-task-more]')?.remove()
    const rows = job.tasks.map((task) => `
      <div class="task-item"><span>${escapeHtml(task.market)} · ${escapeHtml(formatDate(task.chart_date, { month: 'short', day: 'numeric' }))}</span><span class="task-status ${escapeHtml(task.status)}">${escapeHtml(statusLabel(task.status))}</span>
      ${task.error_message ? `<span class="task-error">${escapeHtml(task.error_message)}</span>` : ''}</div>`).join('')
    if (append) target.insertAdjacentHTML('beforeend', rows)
    else target.innerHTML = rows
    if (page * job.task_page_size < job.task_count) {
      target.insertAdjacentHTML('beforeend', `<button class="task-load-more" data-task-more type="button">Show next 50 of ${job.task_count} market/day results</button>`)
      target.querySelector('[data-task-more]').addEventListener('click', () => loadJobTasks(id, page + 1, true))
    }
  } catch (error) {
    console.error('[v0] Could not load job tasks:', error.message)
  }
}

async function handleJobAction(id, action) {
  const card = document.querySelector(`[data-job-card="${CSS.escape(id)}"]`)
  if (action === 'tasks') {
    if (state.expandedJobs.has(id)) state.expandedJobs.delete(id)
    else state.expandedJobs.add(id)
    const list = card?.querySelector('[data-task-list]')
    if (list) list.hidden = !state.expandedJobs.has(id)
    const button = card?.querySelector('[data-job-action="tasks"]')
    if (button) button.textContent = state.expandedJobs.has(id) ? 'Hide task status' : 'View task status'
    if (state.expandedJobs.has(id)) await loadJobTasks(id, 1)
    return
  }
  if (action === 'toggle-markets') {
    const picker = card?.querySelector('[data-retry-picker]')
    if (picker) picker.hidden = !picker.hidden
    return
  }
  try {
    if (action === 'retry') {
      await request(`/api/jobs/${encodeURIComponent(id)}/retry`, { method: 'POST', body: JSON.stringify({ mode: 'failed' }) })
      showToast('Failed market/day tasks were requeued. Successful snapshots were left untouched.')
    } else if (action === 'retry-selected') {
      const markets = [...(card?.querySelectorAll('[data-retry-picker] input:checked') || [])].map((input) => input.value)
      if (!markets.length) {
        showToast('Select at least one market to retry.')
        return
      }
      const response = await request(`/api/jobs/${encodeURIComponent(id)}/retry`, { method: 'POST', body: JSON.stringify({ mode: 'selected', markets }) })
      showToast(`${response.requeued} selected market/day tasks were requeued.`)
    } else {
      await request(`/api/jobs/${encodeURIComponent(id)}/${action}`, { method: 'POST' })
      showToast(action === 'pause' ? 'Collection paused.' : 'Collection resumed.')
    }
    await loadJobs()
  } catch (error) {
    showToast(error.message)
  }
}

async function loadJobs() {
  state.jobs = await request('/api/jobs')
  renderJobs()
}

async function loadSummary() {
  const summary = await request('/api/summary')
  byId('metric-entries').textContent = Number(summary.entries || 0).toLocaleString()
  byId('metric-snapshots').textContent = Number(summary.snapshots || 0).toLocaleString()
  byId('metric-jobs').textContent = Number(summary.active_jobs || 0).toLocaleString()
  byId('metric-date').textContent = summary.latest_date ? formatDate(summary.latest_date, { month: 'short', day: 'numeric' }) : '—'
  const signature = `${summary.entries}:${summary.snapshots}:${summary.latest_date || ''}:${summary.latest_snapshot_at || ''}`
  if (state.snapshotSignature && state.snapshotSignature !== signature) {
    await loadEntities()
    state.page = 1
    await loadEntries()
  }
  state.snapshotSignature = signature
}

function activeFilters() {
  const filters = {
    chart_type: byId('filter-chart').value,
    market: byId('filter-market').value,
    start_date: byId('filter-start').value,
    end_date: byId('filter-end').value,
  }
  const selectedEntity = byId('filter-entity').value
  if (selectedEntity !== '') {
    const entity = state.entities[Number(selectedEntity)]
    if (entity) {
      filters.entity_type = entity.entity_type
      filters.entity_id = entity.entity_id
    }
  }
  return filters
}

function queryEntries() {
  const params = new URLSearchParams({ page: String(state.page), page_size: String(state.pageSize) })
  Object.entries(activeFilters()).forEach(([key, value]) => { if (value) params.set(key, value) })
  return request(`/api/entries?${params.toString()}`)
}

function queryTrends() {
  const params = new URLSearchParams()
  Object.entries(activeFilters()).forEach(([key, value]) => { if (value) params.set(key, value) })
  return request(`/api/trends?${params.toString()}`)
}

function renderEntries() {
  const body = byId('entries-body')
  if (!state.entries.length) {
    body.innerHTML = '<tr><td colspan="6" class="empty-cell">No saved entries match these filters. Run a collection to build the shared archive.</td></tr>'
    return
  }
  body.innerHTML = state.entries.map((entry) => {
    const rank = entry.rank == null ? '—' : `#${Number(entry.rank)}`
    const rankChange = entry.rank_change == null ? '<span class="rank-change new">—</span>' : `<span class="rank-change ${Number(entry.rank_change) > 0 ? 'up' : Number(entry.rank_change) < 0 ? 'down' : 'flat'}">${Number(entry.rank_change) > 0 ? '↑ ' : Number(entry.rank_change) < 0 ? '↓ ' : ''}${Math.abs(Number(entry.rank_change))}</span>`
    const viewChange = entry.view_change == null ? '<span class="view-change flat">—</span>' : `<span class="view-change ${Number(entry.view_change) > 0 ? 'up' : 'flat'}">${Number(entry.view_change) > 0 ? '+' : ''}${formatCompact(entry.view_change)}</span>`
    const thumb = entry.video_id ? `https://i.ytimg.com/vi/${encodeURIComponent(entry.video_id)}/mqdefault.jpg` : ''
    const subtitle = `${escapeHtml(chartLabel(entry.chart_type))} · ${escapeHtml(entry.market)}`
    return `<tr data-entry-row><td><div class="week-market">${escapeHtml(formatDate(entry.snapshot_date, { month: 'short', day: 'numeric', year: '2-digit' }))}<br />${escapeHtml(entry.market)}</div></td><td><div class="entry-cell">${thumb ? `<img class="entry-thumb" src="${thumb}" alt="" loading="lazy" referrerpolicy="no-referrer" />` : '<span class="entry-thumb-fallback" aria-hidden="true">CHART</span>'}<div><div class="entry-title">${escapeHtml(entry.title)}</div><div class="entry-subtitle">${subtitle}</div></div></div></td><td><span class="rank-value">${rank}</span></td><td>${rankChange}</td><td><span class="views-value">${formatCompact(entry.view_count)}</span></td><td>${viewChange}</td></tr>`
  }).join('')
}

function renderTrend() {
  const chart = byId('trend-chart')
  const points = state.trend.map((item) => ({ day: item.snapshot_date, rank: Number(item.average_rank) }))
  byId('trend-caption').textContent = points.length ? `${points.length} saved chart week${points.length === 1 ? '' : 's'} · lower rank is closer to #1` : 'Collect snapshots to reveal a trend.'
  byId('trend-range').textContent = points.length ? `${formatDate(points[0].day, { month: 'short', day: 'numeric' })} — ${formatDate(points.at(-1).day, { month: 'short', day: 'numeric' })}` : '—'
  if (points.length < 2) {
    chart.innerHTML = '<div class="trend-empty">Save at least two weekly snapshots to compare movement.</div>'
    return
  }
  const width = 900
  const height = 140
  const margin = { left: 28, right: 15, top: 16, bottom: 22 }
  const max = Math.max(...points.map((point) => point.rank))
  const min = Math.min(...points.map((point) => point.rank))
  const spread = Math.max(1, max - min)
  const coords = points.map((point, index) => ({
    ...point,
    x: margin.left + index * (width - margin.left - margin.right) / (points.length - 1),
    y: margin.top + (point.rank - min) * (height - margin.top - margin.bottom) / spread,
  }))
  const path = coords.map((point, index) => `${index ? 'L' : 'M'} ${point.x.toFixed(1)} ${point.y.toFixed(1)}`).join(' ')
  const circles = coords.map((point) => `<circle class="trend-point" cx="${point.x}" cy="${point.y}" r="3"><title>${escapeHtml(formatDate(point.day))}: average rank ${point.rank.toFixed(1)}</title></circle>`).join('')
  const labels = [coords[0], coords.at(-1)].map((point) => `<text class="trend-label" x="${point.x}" y="${height - 4}" text-anchor="${point === coords[0] ? 'start' : 'end'}">${escapeHtml(formatDate(point.day, { month: 'short', day: 'numeric' }))}</text>`).join('')
  chart.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Average chart rank across ${points.length} saved weeks"><path class="trend-line" d="${path}" />${circles}${labels}</svg>`
}

async function loadEntries() {
  byId('entries-body').innerHTML = '<tr><td colspan="6" class="loading-cell">Loading the shared archive…</td></tr>'
  try {
    const [response, trendResponse] = await Promise.all([queryEntries(), queryTrends()])
    state.entries = response.items
    state.trend = trendResponse.items
    state.total = response.total
    renderEntries()
    renderTrend()
    const first = state.total ? (state.page - 1) * state.pageSize + 1 : 0
    const last = Math.min(state.page * state.pageSize, state.total)
    byId('entry-count').textContent = state.total ? `${first}–${last} of ${Number(state.total).toLocaleString()} entries` : 'No saved entries'
    const pages = Math.max(1, Math.ceil(state.total / state.pageSize))
    byId('page-indicator').textContent = `Page ${state.page} of ${pages}`
    byId('prev-page').disabled = state.page <= 1
    byId('next-page').disabled = state.page >= pages
  } catch (error) {
    byId('entries-body').innerHTML = `<tr><td colspan="6" class="empty-cell">${escapeHtml(error.message)}</td></tr>`
  }
}

async function loadWorkspace() {
  try {
    state.markets = await request('/api/markets')
    renderMarkets()
    await loadEntities()
    await Promise.all([loadSummary(), loadConfigurations(), loadJobs(), loadEntries()])
    byId('connection-status').textContent = 'DATABASE CONNECTED'
    byId('connection-status').style.color = 'var(--green)'
    clearInterval(state.pollTimer)
    state.pollTimer = setInterval(() => Promise.all([loadJobs(), loadSummary()]).catch(() => {}), 5000)
  } catch (error) {
    byId('connection-status').textContent = 'DATABASE OFFLINE'
    showToast(error.message)
  }
}

async function startJob(event) {
  event.preventDefault()
  const errorNode = byId('job-error')
  errorNode.hidden = true
  const button = byId('start-job-button')
  button.disabled = true
  try {
    const entityType = byId('entity-type').value
    const payload = {
      name: byId('configuration-name').value.trim() || 'Chart collection',
      chart_type: byId('chart-type').value,
      entity_type: entityType,
      entity_id: entityType === 'all' ? 'all' : byId('entity-id').value.trim(),
      markets: selectedMarkets(),
      start_date: byId('start-date').value,
      end_date: byId('end-date').value,
      save_configuration: byId('save-configuration').checked,
    }
    const result = await request('/api/jobs', { method: 'POST', body: JSON.stringify(payload) })
    showToast(`Chart collection started · ${result.total_tasks} chart snapshots queued.`)
    await Promise.all([loadJobs(), loadSummary(), loadConfigurations()])
  } catch (error) {
    errorNode.textContent = error.message
    errorNode.hidden = false
  } finally {
    button.disabled = selectedMarkets().length === 0
  }
}

async function refreshJobs() {
  try {
    await Promise.all([loadJobs(), loadSummary(), loadEntities(), loadEntries()])
  } catch (error) {
    showToast(error.message)
  }
}

function renderPlaylistRows(targetId, items) {
  const body = byId(targetId)
  if (!items.length) {
    body.innerHTML = `<tr><td colspan="4" class="empty-cell">No matching playlist videos were found in the saved chart snapshots.</td></tr>`
    return
  }
  body.innerHTML = items.map((entry) => {
    const thumbnail = entry.video_id ? `https://i.ytimg.com/vi/${encodeURIComponent(entry.video_id)}/mqdefault.jpg` : ''
    return `<tr><td><div class="week-market">${escapeHtml(formatDate(entry.snapshot_date, { month: 'short', day: 'numeric', year: '2-digit' }))}<br />${escapeHtml(entry.market)}</div></td><td><div class="entry-cell">${thumbnail ? `<img class="entry-thumb" src="${thumbnail}" alt="" loading="lazy" referrerpolicy="no-referrer" />` : ''}<div><div class="entry-title">${escapeHtml(entry.title)}</div><div class="entry-subtitle">${escapeHtml(chartLabel(entry.chart_type))}</div></div></div></td><td><span class="rank-value">${entry.rank == null ? '—' : `#${Number(entry.rank)}`}</span></td><td><span class="views-value">${formatCompact(entry.view_count)}</span></td></tr>`
  }).join('')
}

function playlistQuery(history = false) {
  const params = new URLSearchParams({
    playlist: state.playlistReference,
    chart_type: byId('live-chart').value,
    market: byId('live-market').value,
  })
  if (history) {
    params.set('page', String(state.historyPage))
    params.set('page_size', String(state.historyPageSize))
  }
  return params
}

async function loadLiveFilter(event) {
  event?.preventDefault()
  const errorNode = byId('playlist-error')
  errorNode.hidden = true
  state.playlistReference = byId('playlist-input').value.trim()
  if (!state.playlistReference) return
  const button = byId('playlist-submit')
  button.disabled = true
  byId('live-results-body').innerHTML = '<tr><td colspan="4" class="loading-cell">Checking saved chart results…</td></tr>'
  try {
    const result = await request(`/api/live-filter?${playlistQuery()}`)
    renderPlaylistRows('live-results-body', result.items)
    byId('live-result-meta').textContent = `${result.total} MATCHING VIDEOS`
    state.historyPage = 1
    if (location.hash === '#history') await loadPlaylistHistory()
  } catch (error) {
    errorNode.textContent = error.message
    errorNode.hidden = false
    byId('live-results-body').innerHTML = `<tr><td colspan="4" class="empty-cell">${escapeHtml(error.message)}</td></tr>`
  } finally {
    button.disabled = false
  }
}

async function loadPlaylistHistory() {
  const playlist = byId('playlist-input').value.trim() || state.playlistReference
  if (!playlist) {
    byId('history-count').textContent = 'Add a playlist in Live filter to start.'
    return
  }
  state.playlistReference = playlist
  byId('history-body').innerHTML = '<tr><td colspan="4" class="loading-cell">Loading playlist history…</td></tr>'
  try {
    const result = await request(`/api/live-history?${playlistQuery(true)}`)
    renderPlaylistRows('history-body', result.items)
    state.historyTotal = result.total
    const first = result.total ? (state.historyPage - 1) * state.historyPageSize + 1 : 0
    const last = Math.min(state.historyPage * state.historyPageSize, result.total)
    const pages = Math.max(1, Math.ceil(result.total / state.historyPageSize))
    byId('history-count').textContent = result.total ? `${Number(result.total).toLocaleString()} saved playlist matches` : 'No saved playlist matches yet.'
    byId('history-page-indicator').textContent = `Page ${state.historyPage} of ${pages}`
    byId('history-range').textContent = result.total ? `${first}–${last} of ${Number(result.total).toLocaleString()}` : '—'
    byId('history-prev').disabled = state.historyPage <= 1
    byId('history-next').disabled = state.historyPage >= pages
  } catch (error) {
    byId('history-body').innerHTML = `<tr><td colspan="4" class="empty-cell">${escapeHtml(error.message)}</td></tr>`
  }
}

function activatePage(page) {
  const validPage = ['explorer', 'live-filter', 'history'].includes(page) ? page : 'explorer'
  document.querySelectorAll('[data-page-view]').forEach((view) => {
    const active = view.dataset.pageView === validPage
    view.hidden = !active
    view.classList.toggle('is-active', active)
  })
  document.querySelectorAll('[data-page-link]').forEach((link) => {
    const active = link.dataset.pageLink === validPage
    link.classList.toggle('is-active', active)
    if (active) link.setAttribute('aria-current', 'page')
    else link.removeAttribute('aria-current')
  })
  if (validPage === 'history' && state.playlistReference) loadPlaylistHistory()
}

byId('playlist-form').addEventListener('submit', loadLiveFilter)
byId('load-history').addEventListener('click', loadPlaylistHistory)
byId('history-prev').addEventListener('click', () => { state.historyPage = Math.max(1, state.historyPage - 1); loadPlaylistHistory() })
byId('history-next').addEventListener('click', () => { state.historyPage += 1; loadPlaylistHistory() })
document.querySelectorAll('[data-page-link]').forEach((link) => link.addEventListener('click', () => activatePage(link.dataset.pageLink)))
window.addEventListener('hashchange', () => activatePage(location.hash.slice(1)))
activatePage(location.hash.slice(1))

byId('job-form').addEventListener('submit', startJob)
byId('entity-type').addEventListener('change', () => {
  byId('entity-field').hidden = byId('entity-type').value === 'all'
  byId('entity-id').required = byId('entity-type').value !== 'all'
})
byId('filter-form').addEventListener('submit', (event) => { event.preventDefault(); state.page = 1; loadEntries() })
byId('prev-page').addEventListener('click', () => { state.page = Math.max(1, state.page - 1); loadEntries() })
byId('next-page').addEventListener('click', () => { state.page += 1; loadEntries() })
byId('refresh-jobs').addEventListener('click', refreshJobs)
byId('refresh-configurations').addEventListener('click', () => loadConfigurations().catch((error) => showToast(error.message)))
byId('start-date').addEventListener('change', () => { if (!byId('end-date').value || byId('end-date').value < byId('start-date').value) byId('end-date').value = byId('start-date').value })
byId('save-configuration').addEventListener('change', (event) => { byId('configuration-name').disabled = !event.currentTarget.checked })

setFormDates()
loadWorkspace()
