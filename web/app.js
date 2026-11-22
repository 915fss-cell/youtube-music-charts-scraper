const state = { tracked: [], chart: [], selected: new Set() }
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
    headers: { 'Content-Type': 'application/json', ...options.headers },
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.detail || 'Something went wrong. Please try again.')
  return body
}

function imageMarkup(source, title, className = 'song-thumb') {
  return source
    ? `<img class="${className}" src="${escapeHtml(source)}" alt="" loading="lazy" referrerpolicy="no-referrer" />`
    : `<span class="song-thumb-fallback" aria-hidden="true">♫</span>`
}

function formatViews(value) {
  if (value == null || value === '' || !Number.isFinite(Number(value))) return '—'
  return `${new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(Number(value))} views`
}

function renderTracked() {
  const body = byId('tracked-body')
  const totalSnapshots = state.tracked.reduce((total, item) => total + Number(item.snapshot_count || 0), 0)
  const lastDate = state.tracked.map((item) => item.snapshot_date).filter(Boolean).sort().at(-1)
  byId('tracked-count').textContent = String(state.tracked.length).padStart(2, '0')
  byId('heading-count').textContent = String(state.tracked.length).padStart(2, '0')
  byId('snapshot-count').textContent = totalSnapshots.toLocaleString()
  byId('last-updated').textContent = lastDate ? new Date(`${lastDate}T12:00:00`).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : 'Not yet'

  if (!state.tracked.length) {
    body.innerHTML = '<tr><td colspan="5"><div class="empty-state"><div class="empty-glyph" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M9 18V5l12-2v13M9 8l12-2M5 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm12-3a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z" /></svg></div><strong>Your shortlist starts here.</strong><p>Explore popular music videos and save the ones you want to follow.</p></div></td></tr>'
    return
  }

  body.innerHTML = state.tracked.map((item) => {
    const artists = Array.isArray(item.artists) ? item.artists.join(', ') : ''
    const chartPosition = item.rank == null ? '—' : `#${escapeHtml(item.rank)}`
    const trackedDays = item.snapshot_count ? `${escapeHtml(item.snapshot_count)} days` : '—'
    return `<tr>
      <td><div class="song-cell">${imageMarkup(item.thumbnail_url, item.title)}<div><div class="song-title">${escapeHtml(item.title)}</div><div class="song-artists">${escapeHtml(artists)}</div></div></div></td>
      <td><span class="rank-chip">${chartPosition}</span></td>
      <td><span class="views-value">${escapeHtml(formatViews(item.weekly_views))}</span></td>
      <td><span class="snapshot-chip">${trackedDays}</span></td>
      <td><div class="row-actions"><button class="remove-button" type="button" data-remove="${Number(item.id)}" aria-label="Remove ${escapeHtml(item.title)} from tracked picks" title="Remove from tracked picks">×</button></div></td>
    </tr>`
  }).join('')

  body.querySelectorAll('[data-remove]').forEach((button) => button.addEventListener('click', () => removeTracked(button.dataset.remove)))
}

function renderChart() {
  const query = byId('chart-search').value.trim().toLocaleLowerCase()
  const trackedUrls = new Set(state.tracked.map((item) => item.video_url))
  const items = state.chart.filter((item) => `${item.title} ${item.artists.join(' ')}`.toLocaleLowerCase().includes(query))
  byId('chart-subtitle').textContent = state.chart.length ? `${state.chart.length} popular music videos in the US. Select any you want to track.` : 'Choose the videos you want to follow.'
  byId('selection-count').textContent = `${state.selected.size} selected`
  byId('save-selection-button').disabled = state.selected.size === 0

  if (!items.length) {
    byId('chart-list').innerHTML = `<div class="chart-empty">${state.chart.length ? 'No videos match that search.' : 'Load popular music videos to start choosing.'}</div>`
    return
  }

  byId('chart-list').innerHTML = items.map((item, index) => {
    const tracked = trackedUrls.has(item.url)
    const checked = state.selected.has(item.url)
    return `<label class="chart-row">
      <input type="checkbox" data-select="${escapeHtml(item.url)}" ${checked ? 'checked' : ''} ${tracked ? 'disabled' : ''} aria-label="Select ${escapeHtml(item.title)}" />
      <span class="chart-rank">${escapeHtml(item.rank || index + 1)}</span>
      ${imageMarkup(item.thumbnail, item.title)}
      <span><span class="song-title">${escapeHtml(item.title)}</span><span class="song-artists">${escapeHtml(item.artists.join(', '))}</span></span>
      ${tracked ? '<span class="already-tracked">TRACKING</span>' : `<span class="chart-row-meta">${escapeHtml(formatViews(item.metrics?.view_count))}</span>`}
    </label>`
  }).join('')

  byId('chart-list').querySelectorAll('[data-select]').forEach((checkbox) => {
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) state.selected.add(checkbox.dataset.select)
      else state.selected.delete(checkbox.dataset.select)
      byId('selection-count').textContent = `${state.selected.size} selected`
      byId('save-selection-button').disabled = state.selected.size === 0
    })
  })
}

async function loadTracked() {
  try {
    state.tracked = await request('/api/tracked')
    renderTracked()
    byId('connection-status').textContent = 'DATABASE CONNECTED'
    byId('connection-status').classList.remove('error')
  } catch (error) {
    byId('tracked-body').innerHTML = `<tr><td colspan="5" class="empty-cell">${escapeHtml(error.message)}</td></tr>`
    byId('connection-status').textContent = 'DATABASE OFFLINE'
    byId('connection-status').classList.add('error')
  }
}

async function loadChart() {
  const button = byId('load-chart-button')
  const label = button.querySelector('span:nth-child(2)')
  button.disabled = true
  label.textContent = 'Reading the chart…'
  byId('chart-list').innerHTML = '<div class="chart-empty">Loading popular music videos from YouTube…</div>'
  try {
    state.chart = await request('/api/chart')
    state.selected.clear()
    byId('chart-section').hidden = false
    renderChart()
    byId('chart-section').scrollIntoView({ behavior: 'smooth', block: 'start' })
    if (!state.chart.length) showToast('The chart loaded without any entries. Try again shortly.')
  } catch (error) {
    byId('chart-list').innerHTML = `<div class="chart-empty">${escapeHtml(error.message)}</div>`
    byId('chart-section').hidden = false
    showToast(error.message)
  } finally {
    button.disabled = false
    label.textContent = 'Explore popular music videos'
  }
}

async function saveSelection() {
  const selected = state.chart.filter((item) => state.selected.has(item.url))
  const button = byId('save-selection-button')
  button.disabled = true
  button.querySelector('span').textContent = 'Saving…'
  try {
    const result = await request('/api/tracked', { method: 'POST', body: JSON.stringify({ items: selected }) })
    showToast(`${result.saved} ${result.saved === 1 ? 'pick' : 'picks'} saved with today\'s snapshot.`)
    state.selected.clear()
    await loadTracked()
    renderChart()
  } catch (error) {
    showToast(error.message)
  } finally {
    button.querySelector('span').textContent = 'Save selected'
    button.disabled = state.selected.size === 0
  }
}

async function removeTracked(id) {
  try {
    await request(`/api/tracked/${encodeURIComponent(id)}`, { method: 'DELETE' })
    state.tracked = state.tracked.filter((item) => String(item.id) !== String(id))
    renderTracked()
    renderChart()
    showToast('Removed from your tracked picks.')
  } catch (error) {
    showToast(error.message)
  }
}

async function refreshSnapshots() {
  const button = byId('refresh-button')
  button.disabled = true
  button.setAttribute('aria-busy', 'true')
  try {
    const result = await request('/api/refresh', { method: 'POST' })
    await loadTracked()
    showToast(result.updated ? `Updated ${result.updated} view ${result.updated === 1 ? 'snapshot' : 'snapshots'}.` : 'View counts refreshed. No tracked videos were found.')
  } catch (error) {
    showToast(error.message)
  } finally {
    button.disabled = false
    button.removeAttribute('aria-busy')
  }
}

byId('load-chart-button').addEventListener('click', loadChart)
byId('open-chart-button').addEventListener('click', () => {
  byId('chart-section').hidden = false
  byId('chart-section').scrollIntoView({ behavior: 'smooth', block: 'start' })
  if (!state.chart.length) loadChart()
})
byId('close-chart-button').addEventListener('click', () => { byId('chart-section').hidden = true })
byId('save-selection-button').addEventListener('click', saveSelection)
byId('refresh-button').addEventListener('click', refreshSnapshots)
byId('chart-search').addEventListener('input', renderChart)
loadTracked()
