/* fnack — Modern Dashboard, Artist Management & Socket.IO Client */

const socket = io();

// ----- Global In-Memory Search Cache & Abort Controller -----
const searchCache = new Map();
let currentSearchAbort = null;
let searchDebounceTimer = null;

// ----- Utilities -----
function escapeHtml(str) {
  if (!str) return '';
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function formatDuration(seconds) {
  if (!seconds || seconds <= 0) return '—';
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s < 10 ? '0' : ''}${s}`;
}

function formatBytes(bytes) {
  if (!bytes || bytes <= 0) return '0 MB';
  const mb = bytes / (1024 * 1024);
  if (mb >= 1024) return (mb / 1024).toFixed(2) + ' GB';
  return mb.toFixed(1) + ' MB';
}

function statusBadgeHtml(status, errorMessage = null) {
  const map = {
    completed: { cls: 'badge-completed', icon: 'fa-check', label: 'Downloaded' },
    downloading: { cls: 'badge-downloading', icon: 'fa-spinner fa-spin', label: 'Downloading' },
    queued: { cls: 'badge-queued', icon: 'fa-clock', label: 'Queued' },
    missing: { cls: 'badge-missing', icon: 'fa-arrow-down', label: 'Missing' },
    failed: { cls: 'badge-failed', icon: 'fa-times-circle', label: 'Failed' },
    cancelled: { cls: 'badge-failed', icon: 'fa-ban', label: 'Cancelled' },
  };
  const item = map[status] || { cls: 'badge-missing', icon: 'fa-question', label: status };
  const titleAttr = errorMessage ? `title="${escapeHtml(errorMessage)}"` : '';
  return `<span class="badge-status ${item.cls}" ${titleAttr}><i class="fas ${item.icon}"></i> ${item.label}</span>`;
}

// ----- Toast Notifications -----
function showToast(message, type = 'info') {
  const container = document.getElementById('toastContainer');
  if (!container) return;

  const bg = type === 'error' ? 'bg-danger' : type === 'success' ? 'bg-success' : 'bg-info text-dark';
  const icon = type === 'error' ? 'fa-exclamation-circle' : type === 'success' ? 'fa-check-circle' : 'fa-info-circle';
  const id = 'toast_' + Date.now();

  const html = `
    <div id="${id}" class="toast align-items-center text-white ${bg} border-0 shadow-lg mb-2" role="alert" aria-live="assertive" aria-atomic="true">
      <div class="d-flex">
        <div class="toast-body d-flex align-items-center gap-2">
          <i class="fas ${icon}"></i>
          <span>${escapeHtml(message)}</span>
        </div>
        <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
      </div>
    </div>`;

  container.insertAdjacentHTML('beforeend', html);
  const el = document.getElementById(id);
  const toast = new bootstrap.Toast(el, { delay: 4000 });
  toast.show();
  el.addEventListener('hidden.bs.toast', () => el.remove());
}

// ----- Confirmation Modal -----
let _customModalCallback = null;

function showConfirmModal(title, message, onConfirm, confirmText = 'Confirm', btnClass = 'btn-brand') {
  const modal = document.getElementById('confirmModal');
  if (!modal) return;
  document.getElementById('confirmModalTitle').textContent = title;
  document.getElementById('confirmModalBody').innerHTML = message;
  const btn = document.getElementById('confirmModalOkBtn');
  btn.textContent = confirmText;
  btn.className = `btn ${btnClass}`;
  _customModalCallback = onConfirm;
  modal.classList.remove('d-none');
}

function hideConfirmModal() {
  const modal = document.getElementById('confirmModal');
  if (modal) modal.classList.add('d-none');
  _customModalCallback = null;
}

// ----- Fast Search with AbortController & Client Caching -----
function initArtistSearch() {
  const input = document.getElementById('artistSearch');
  const dropdown = document.getElementById('searchDropdown');
  const spinner = document.getElementById('searchSpinner');
  if (!input || !dropdown) return;

  input.addEventListener('input', function () {
    const query = this.value.trim();
    clearTimeout(searchDebounceTimer);

    if (query.length < 2) {
      dropdown.classList.add('d-none');
      if (spinner) spinner.classList.add('d-none');
      return;
    }

    if (spinner) spinner.classList.remove('d-none');

    searchDebounceTimer = setTimeout(async () => {
      // Check client cache first
      if (searchCache.has(query.toLowerCase())) {
        renderSearchResults(searchCache.get(query.toLowerCase()));
        if (spinner) spinner.classList.add('d-none');
        return;
      }

      // Cancel obsolete pending request
      if (currentSearchAbort) {
        currentSearchAbort.abort();
      }
      currentSearchAbort = new AbortController();

      try {
        const resp = await fetch(`/api/search-artist?q=${encodeURIComponent(query)}`, {
          signal: currentSearchAbort.signal,
        });
        const results = await resp.json();
        searchCache.set(query.toLowerCase(), results);
        renderSearchResults(results);
      } catch (err) {
        if (err.name !== 'AbortError') {
          console.error('Search error:', err);
        }
      } finally {
        if (spinner) spinner.classList.add('d-none');
      }
    }, 320);
  });

  function renderSearchResults(results) {
    if (!Array.isArray(results) || results.length === 0) {
      dropdown.innerHTML = '<div class="p-3 text-secondary text-center small">No artists found</div>';
      dropdown.classList.remove('d-none');
      return;
    }

    let html = '';
    for (const a of results) {
      html += `
        <div class="search-item" data-artist='${escapeHtml(JSON.stringify(a))}'>
          ${a.image_url
            ? `<img src="${a.image_url}" class="search-thumb" alt="">`
            : `<div class="search-thumb d-flex align-items-center justify-content-center bg-secondary"><i class="fas fa-user text-white-50"></i></div>`
          }
          <div class="flex-grow-1 min-w-0">
            <div class="fw-semibold text-truncate small">${escapeHtml(a.name)}</div>
            <div class="text-secondary" style="font-size: 0.72rem;">${a.nb_album || 0} releases${a.nb_fan !== undefined ? ` · ${Number(a.nb_fan).toLocaleString()} fans` : ''} · <span class="font-monospace text-dim">ID: ${a.id}</span></div>
          </div>
          <button class="btn btn-sm btn-outline-danger py-0 px-2 small" style="font-size:0.75rem;"><i class="fas fa-plus me-1"></i>Add</button>
        </div>`;
    }

    dropdown.innerHTML = html;
    dropdown.classList.remove('d-none');
  }

  dropdown.addEventListener('click', (e) => {
    const item = e.target.closest('.search-item');
    if (!item) return;
    dropdown.classList.add('d-none');
    input.value = '';
    try {
      const artistData = JSON.parse(item.dataset.artist);
      openAddArtistModal(artistData);
    } catch (err) {
      console.error('Parse artist data failed', err);
    }
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('.search-wrapper')) {
      dropdown.classList.add('d-none');
    }
  });
}

// ----- Add Artist Modal Options -----
let _pendingAddArtistData = null;

function openAddArtistModal(artistData) {
  _pendingAddArtistData = artistData;
  const titleEl = document.getElementById('addArtistModalTitle');
  if (titleEl) titleEl.textContent = `Add "${artistData.name}"`;
  const imgEl = document.getElementById('addArtistImg');
  if (imgEl) imgEl.src = artistData.image_url || '';
  const nameEl = document.getElementById('addArtistName');
  if (nameEl) nameEl.textContent = artistData.name;

  const setChecked = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.checked = val;
  };

  // Defaults
  setChecked('filterRemixes', true);
  setChecked('filterLofi', true);
  setChecked('filterLive', true);
  setChecked('filterCompilations', true);
  setChecked('incAlbums', true);
  setChecked('incSingles', true);
  setChecked('incCompilations', false);
  setChecked('optMonitored', true);
  setChecked('optAutoDownload', false);

  const modalEl = document.getElementById('addArtistModal');
  if (modalEl) modalEl.classList.remove('d-none');
}

function hideAddArtistModal() {
  const modalEl = document.getElementById('addArtistModal');
  if (modalEl) modalEl.classList.add('d-none');
  _pendingAddArtistData = null;
}

async function confirmAddArtist() {
  if (!_pendingAddArtistData) return;
  const artistData = _pendingAddArtistData;
  const isChecked = (id, def = true) => {
    const el = document.getElementById(id);
    return el ? el.checked : def;
  };
  const payload = {
    id: artistData.id,
    filter_remixes: isChecked('filterRemixes', true),
    filter_lofi: isChecked('filterLofi', true),
    filter_live: isChecked('filterLive', true),
    filter_compilations: isChecked('filterCompilations', true),
    include_albums: isChecked('incAlbums', true),
    include_singles: isChecked('incSingles', true),
    include_compilations: isChecked('incCompilations', false),
    monitored: isChecked('optMonitored', true),
    auto_download: isChecked('optAutoDownload', false),
  };

  hideAddArtistModal();

  try {
    const resp = await fetch('/api/add-artist', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (resp.ok) {
      showToast(data.message, 'success');
      if (document.getElementById('artistsDashboardGrid')) {
        loadDashboardArtists();
      }
    } else {
      showToast(data.error || 'Failed to add artist', 'error');
    }
  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
  }
}

// ----- Dashboard Artists Loader -----
let _libraryArtists = [];

function applyLibraryFilter(artists) {
  const input = document.getElementById('libraryFilterInput');
  const q = (input ? input.value.trim() : '').toLowerCase();
  if (!q) return artists;
  return artists.filter(a => (a.name || '').toLowerCase().includes(q));
}

function initLibraryFilter() {
  const input = document.getElementById('libraryFilterInput');
  if (!input) return;
  input.addEventListener('input', () => {
    if (_libraryArtists.length) {
      renderDashboardArtists(applyLibraryFilter(_libraryArtists));
    }
  });
}

async function loadDashboardArtists() {
  const grid = document.getElementById('artistsDashboardGrid');
  if (!grid) return;

  try {
    const [respArtists, respStats] = await Promise.all([
      fetch('/api/artists'),
      fetch('/api/stats'),
    ]);
    const artists = await respArtists.json();
    _libraryArtists = artists;
    renderDashboardArtists(applyLibraryFilter(artists));

    if (respStats.ok) {
      const stats = await respStats.json();
      updateDashboardStats(stats, artists);
    }
  } catch (err) {
    console.error('Failed to load artists:', err);
  }
}

function renderDashboardArtists(artists) {
  const grid = document.getElementById('artistsDashboardGrid');
  if (!grid) return;

  if (!Array.isArray(artists) || artists.length === 0) {
    grid.innerHTML = `
      <div class="col-12 text-center text-secondary py-5">
        <i class="fas fa-compact-disc fa-3x mb-3 text-secondary d-block"></i>
        <h5>Your library is empty</h5>
        <p class="small text-secondary">Search for an artist above to start downloading their discography in FLAC.</p>
      </div>`;
    return;
  }

  let html = '';
  for (const a of artists) {
    const isSyncing = a.sync_status === 'syncing';
    html += `
      <div class="col-6 col-md-4 col-lg-3 col-xl-2">
        <div class="artist-grid-card h-100" onclick="window.location.href='/artist/${a.id}'">
          <div class="artist-card-img-wrap">
            ${a.image_url
              ? `<img src="${a.image_url}" class="artist-card-img" alt="${escapeHtml(a.name)}" loading="lazy" width="200" height="200">`
              : `<div class="artist-card-placeholder"><i class="fas fa-user"></i></div>`
            }
            ${isSyncing ? `
              <div class="artist-sync-overlay">
                <div class="spinner-border spinner-border-sm text-cyan" role="status"></div>
                <span>Syncing...</span>
              </div>` : ''
            }
          </div>
          <div class="artist-card-body">
            <h6 class="artist-card-title mb-1" title="${escapeHtml(a.name)}">${escapeHtml(a.name)}</h6>
            <div class="d-flex align-items-center justify-content-center gap-2 mb-1">
              <span class="badge ${a.monitored ? 'bg-success-subtle text-success' : 'bg-secondary-subtle text-secondary'}" style="font-size:0.68rem;">
                <i class="fas ${a.monitored ? 'fa-eye' : 'fa-eye-slash'} me-1"></i>${a.monitored ? 'Monitored' : 'Unmonitored'}
              </span>
            </div>
            <small class="text-secondary d-block" style="font-size:0.75rem;">
              ${a.downloaded_tracks} / ${a.total_tracks} tracks (${a.percent_downloaded}%)
            </small>
            <div class="progress mt-2" style="height: 4px; background: rgba(255,255,255,0.05);">
              <div class="progress-bar ${a.percent_downloaded === 100 ? 'bg-success' : 'bg-info'}" style="width: ${a.percent_downloaded}%"></div>
            </div>
          </div>
        </div>
      </div>`;
  }
  grid.innerHTML = html;
}

function updateDashboardStats(stats, artists = []) {
  const elArtists = document.getElementById('statTotalArtists');
  const elTracks = document.getElementById('statDownloadedTracks');
  const elFailed = document.getElementById('statFailedTracks');
  const elSize = document.getElementById('statCatalogueSize');
  const elFailedBadge = document.getElementById('failedBadgeHome');
  const elMobileSize = document.getElementById('mobileMenuSize');
  const elMobileFailed = document.getElementById('mobileMenuFailed');

  if (stats) {
    if (elArtists) elArtists.textContent = stats.total_artists ?? artists.length;
    if (elTracks) elTracks.textContent = stats.downloaded_tracks ?? 0;
    if (elFailed) elFailed.textContent = stats.failed_tracks ?? 0;
    if (elSize) elSize.textContent = stats.total_size_formatted || '0 MB';
    if (elFailedBadge) elFailedBadge.textContent = stats.failed_tracks ?? 0;
    if (elMobileSize) elMobileSize.textContent = stats.total_size_formatted || '0 MB';
    if (elMobileFailed) elMobileFailed.textContent = stats.failed_tracks ?? 0;
  }
}

async function retryAllFailedFromHome() {
  const btn = document.getElementById('globalRetryFailedBtn');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Retrying...';
  }
  try {
    const resp = await fetch('/api/queue/retry-failed', { method: 'POST' });
    const data = await resp.json();
    if (resp.ok) {
      showToast(data.message || 'Re-queued failed tracks', 'success');
      loadDashboardArtists();
    } else {
      showToast(data.error || 'Failed to retry failed tracks', 'error');
    }
  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '<i class="fas fa-redo me-1"></i><span>Retry All Failed (<span id="failedBadgeHome">0</span>)</span>';
      loadDashboardArtists();
    }
  }
}

// ----- Dedicated Artist Detail Page -----
async function loadArtistDetailPage(artistId) {
  const container = document.getElementById('artistPageContainer');
  if (!container) return;

  try {
    const resp = await fetch(`/api/artist/${artistId}`);
    if (!resp.ok) {
      container.innerHTML = '<div class="alert alert-danger">Artist not found</div>';
      return;
    }
    const artist = await resp.json();
    renderArtistDetailPage(artist);
  } catch (err) {
    container.innerHTML = `<div class="alert alert-danger">Error: ${escapeHtml(err.message)}</div>`;
  }
}

function renderArtistDetailPage(artist) {
  const container = document.getElementById('artistPageContainer');
  if (!container) return;

  window._currentArtistId = artist.id;
  window._currentArtistName = artist.name;

  // Split albums by type
  const studioAlbums = artist.albums.filter(a => a.record_type === 'album');
  const singlesAndEps = artist.albums.filter(a => a.record_type === 'single' || a.record_type === 'ep');
  const compilations = artist.albums.filter(a => a.record_type === 'compile');
  const unmatched = artist.albums.filter(a => a.record_type === 'other');

  let html = `
    <!-- Hero Header -->
    <div class="artist-hero">
      <div class="container">
        <div class="d-flex flex-column flex-md-row align-items-center gap-4">
          ${artist.image_url
            ? `<img src="${artist.image_url}" class="artist-hero-avatar" alt="${escapeHtml(artist.name)}">`
            : `<div class="artist-hero-avatar d-flex align-items-center justify-content-center bg-secondary"><i class="fas fa-user fa-3x"></i></div>`
          }
          <div class="flex-grow-1 text-center text-md-start">
            <h1 class="fw-bold mb-1">${escapeHtml(artist.name)}</h1>
            <div class="d-flex flex-wrap align-items-center justify-content-center justify-content-md-start gap-2 mb-3">
              <span class="badge ${artist.monitored ? 'bg-success-subtle text-success' : 'bg-secondary-subtle text-secondary'} fs-6">
                <i class="fas ${artist.monitored ? 'fa-eye' : 'fa-eye-slash'} me-1"></i>${artist.monitored ? 'Monitored' : 'Unmonitored'}
              </span>
              <span class="badge bg-dark border border-secondary text-secondary">
                ${artist.total_albums} Releases · ${artist.total_tracks} Tracks
              </span>
              <span class="badge bg-dark border border-secondary text-secondary">
                ${formatBytes(artist.total_size_bytes)}
              </span>
            </div>
            <div class="d-flex flex-wrap gap-2 justify-content-center justify-content-md-start">
              <button class="btn btn-brand btn-sm" onclick="downloadArtistMissing(${artist.id})">
                <i class="fas fa-download me-1"></i>Download Missing (${artist.total_tracks - artist.downloaded_tracks})
              </button>
              <button class="btn btn-outline-secondary btn-sm" onclick="syncArtistDiscography(${artist.id})">
                <i class="fas fa-sync me-1"></i>Sync Discography
              </button>
              <button class="btn btn-outline-secondary btn-sm" onclick="openArtistFiltersModal(${artist.id})">
                <i class="fas fa-sliders-h me-1"></i>Filters
              </button>
              <button class="btn btn-outline-danger btn-sm" onclick="confirmDeleteArtist(${artist.id}, '${escapeHtml(artist.name)}')">
                <i class="fas fa-trash me-1"></i>Delete
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Main Discography Accordions -->
    <div class="container pb-5">
      ${renderAlbumSection('Studio Albums', studioAlbums, 'albums')}
      ${renderAlbumSection('Singles & EPs', singlesAndEps, 'singles')}
      ${compilations.length ? renderAlbumSection('Compilations', compilations, 'compilations') : ''}
      ${unmatched.length ? renderAlbumSection('Unmatched Local Files', unmatched, 'unmatched') : ''}
    </div>`;

  container.innerHTML = html;
}

function renderAlbumSection(title, albums, prefix) {
  if (!albums || albums.length === 0) return '';
  return `
    <div class="mb-4">
      <h5 class="fw-bold mb-3 text-secondary text-uppercase" style="letter-spacing: 0.5px; font-size: 0.85rem;">${title} (${albums.length})</h5>
      ${albums.map((al, idx) => renderAlbumAccordionCard(al, `${prefix}_${idx}`)).join('')}
    </div>`;
}

function renderAlbumAccordionCard(album, collapseId) {
  const isAllDownloaded = album.is_downloaded || (album.downloaded_count === album.track_count && album.track_count > 0);
  const statusCls = isAllDownloaded ? 'badge-completed' : album.downloaded_count > 0 ? 'badge-downloading' : 'badge-missing';
  const statusLabel = isAllDownloaded ? 'Downloaded' : `${album.downloaded_count}/${album.track_count} Tracks`;
  const isMonitored = album.monitored !== false;

  return `
    <div class="album-card" id="album_card_${album.id}">
      <div class="album-header" data-bs-toggle="collapse" data-bs-target="#${collapseId}">
        <img src="${album.cover_url || ''}" class="album-cover-thumb" onerror="this.src='/static/placeholder.png';">
        <div class="flex-grow-1 min-w-0">
          <div class="d-flex align-items-center gap-2">
            <span class="fw-bold text-truncate">${escapeHtml(album.name)}</span>
            <span class="text-secondary small">(${album.year || '—'})</span>
            ${!isMonitored ? '<span class="badge badge-disabled small py-0 px-2" style="font-size:0.7rem;">Disabled</span>' : ''}
          </div>
          <div class="text-secondary small">${album.track_count} track${album.track_count !== 1 ? 's' : ''} · ${formatBytes(album.size_bytes)}</div>
        </div>
        <div class="d-flex align-items-center gap-2" onclick="event.stopPropagation();">
          <span class="badge-status ${statusCls}">${statusLabel}</span>
          <button class="btn btn-sm ${isMonitored ? 'btn-outline-secondary' : 'btn-outline-warning'} btn-icon" title="${isMonitored ? 'Disable album auto-download' : 'Enable album auto-download'}" onclick="toggleAlbumMonitor(${album.id})">
            <i class="fas ${isMonitored ? 'fa-eye' : 'fa-eye-slash text-warning'}"></i>
          </button>
          <button class="btn btn-sm btn-outline-secondary btn-icon" title="Download album" onclick="downloadAlbum(${album.id})">
            <i class="fas fa-download"></i>
          </button>
          <button class="btn btn-sm btn-outline-danger btn-icon" title="Delete album" onclick="confirmDeleteAlbum(${album.id}, '${escapeHtml(album.name)}')">
            <i class="fas fa-trash"></i>
          </button>
        </div>
        <i class="fas fa-chevron-down text-secondary ms-2"></i>
      </div>
      <div id="${collapseId}" class="collapse show">
        <div class="table-responsive">
          <table class="track-table">
            <thead>
              <tr>
                <th style="width: 40px;">#</th>
                <th>Title</th>
                <th style="width: 75px;">Time</th>
                <th style="width: 130px;" class="d-none-mobile">ISRC</th>
                <th style="width: 120px;">Status</th>
                <th style="width: 140px;" class="d-none-mobile">File Details</th>
                <th style="width: 150px;" class="text-end">Actions</th>
              </tr>
            </thead>
            <tbody>
              ${album.tracks.map(t => renderTrackRow(t, album.name)).join('')}
            </tbody>
          </table>
        </div>
      </div>
    </div>`;
}

function renderTrackRow(track, albumName = '') {
  const isDownloading = track.status === 'downloading';
  const isQueued = track.status === 'queued';
  const isMonitored = track.monitored !== false;
  const safeTitle = (track.title || '').replace(/'/g, "\\'");
  const safeAlbum = (albumName || '').replace(/'/g, "\\'");
  const safeArtist = (window._currentArtistName || '').replace(/'/g, "\\'");
  let cautionInfo = null;
  try { cautionInfo = track.caution_info ? JSON.parse(track.caution_info) : null; } catch (e) { cautionInfo = null; }
  const cautionTitle = cautionInfo ? `Matched to '${cautionInfo.matched_title || '?'}' by ${(cautionInfo.matched_artists || []).join(', ') || '?'} (score ${cautionInfo.score})` : 'AcoustID: this file is a different song';

  return `
    <tr id="track_row_${track.id}" class="${!isMonitored ? 'track-disabled' : ''}">
      <td class="text-secondary small">${track.track_number || '—'}</td>
      <td>
        <div class="fw-semibold text-truncate" title="${escapeHtml(track.title)}">
          ${escapeHtml(track.title)}
          ${!isMonitored ? '<span class="badge badge-disabled ms-1 py-0 px-1" style="font-size:0.65rem;">Disabled</span>' : ''}
        </div>
        ${isDownloading ? `
          <div class="progress mt-1" style="height: 3px;">
            <div class="progress-bar bg-info progress-bar-striped progress-bar-animated" style="width: ${Math.max(5, track.progress || 0)}%"></div>
          </div>` : ''
        }
      </td>
      <td class="text-secondary small">${formatDuration(track.duration)}</td>
      <td class="text-secondary small font-monospace d-none-mobile" style="font-size:0.75rem;">${track.isrc || '—'}</td>
      <td id="track_status_${track.id}">
        ${statusBadgeHtml(track.status, track.error_message)}
        ${track.caution ? `<div class="mt-1"><span class="badge badge-caution" title="${escapeHtml(cautionTitle)}"><i class="fas fa-exclamation-triangle me-1"></i>Caution: matched to '${escapeHtml(cautionInfo ? cautionInfo.matched_title || '?' : '?')}'</span></div>` : ''}
      </td>
      <td class="small text-secondary text-truncate d-none-mobile" style="max-width: 140px;" title="${escapeHtml(track.local_path || '')}">
        ${track.is_downloaded ? `<span class="badge bg-secondary text-uppercase">${track.file_format || 'FLAC'}</span> ${formatBytes(track.size_bytes)}` : '—'}
      </td>
      <td class="text-end">
        <div class="d-inline-flex gap-1">
          <!-- Manual Match Button -->
          <button class="btn btn-sm btn-outline-secondary btn-icon" title="Manual Match / Fix Song with Custom URL" onclick="openManualMatchModal(${track.id}, '${safeTitle}', '${safeArtist}', '${safeAlbum}')">
            <i class="fas fa-search"></i>
          </button>
          <button class="btn btn-sm ${isMonitored ? 'btn-outline-secondary' : 'btn-outline-warning'} btn-icon" title="${isMonitored ? 'Disable / Don\'t download this song' : 'Enable / Monitor this song'}" onclick="toggleTrackMonitor(${track.id})">
            <i class="fas ${isMonitored ? 'fa-eye' : 'fa-eye-slash text-warning'}"></i>
          </button>
          ${!track.is_downloaded && !isDownloading && !isQueued ? `
            <button class="btn btn-sm btn-outline-info btn-icon" title="Download track" onclick="downloadTrack(${track.id})">
              <i class="fas fa-download"></i>
            </button>` : ''
          }
          ${isDownloading || isQueued ? `
            <button class="btn btn-sm btn-outline-danger btn-icon" title="Cancel download" onclick="cancelTrack(${track.id})">
              <i class="fas fa-ban"></i>
            </button>` : ''
          }
          ${track.is_downloaded ? `
            <button class="btn btn-sm btn-outline-danger btn-icon" title="Delete file" onclick="deleteTrack(${track.id}, '${escapeHtml(track.title)}')">
              <i class="fas fa-trash"></i>
            </button>` : ''
          }
          ${track.caution ? `
            <button class="btn btn-sm btn-outline-success btn-icon" title="Keep — accept it as '${escapeHtml(cautionInfo && cautionInfo.matched_title ? cautionInfo.matched_title : '?')}' and re-tag" onclick="resolveCaution(${track.id}, 'keep')">
              <i class="fas fa-check"></i>
            </button>
            <button class="btn btn-sm btn-outline-danger btn-icon" title="Delete this mismatched file" onclick="resolveCaution(${track.id}, 'delete')">
              <i class="fas fa-trash"></i>
            </button>` : ''
          }
          ${track.status === 'failed' ? `
            <button class="btn btn-sm btn-outline-warning btn-icon" title="Retry download" onclick="downloadTrack(${track.id})">
              <i class="fas fa-redo"></i>
            </button>` : ''
          }
        </div>
      </td>
    </tr>`;
}

// ----- Manual Match / Fix Song Modal Handlers -----
let _currentManualMatchTrackId = null;

function openManualMatchModal(trackId, title, artistName, albumName) {
  _currentManualMatchTrackId = trackId;
  const modal = document.getElementById('manualMatchModal');
  if (!modal) return;
  document.getElementById('manualMatchTrackTitle').textContent = title || 'Song';
  document.getElementById('manualMatchTrackArtist').textContent = artistName ? `${artistName}${albumName ? ` — ${albumName}` : ''}` : '';
  const input = document.getElementById('manualMatchUrlInput');
  input.value = '';
  modal.classList.remove('d-none');
  setTimeout(() => input.focus(), 50);

  const submitBtn = document.getElementById('manualMatchSubmitBtn');
  if (submitBtn) {
    submitBtn.onclick = submitManualMatch;
  }
}

function hideManualMatchModal() {
  const modal = document.getElementById('manualMatchModal');
  if (modal) modal.classList.add('d-none');
  _currentManualMatchTrackId = null;
}

async function submitManualMatch() {
  if (!_currentManualMatchTrackId) return;
  const input = document.getElementById('manualMatchUrlInput');
  const url = input.value.trim();
  if (!url) {
    showToast('Please enter a Spotify, YouTube, YouTube Music, or Deezer URL', 'error');
    return;
  }

  const submitBtn = document.getElementById('manualMatchSubmitBtn');
  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Fetching...';
  }

  try {
    const resp = await fetch(`/api/track/${_currentManualMatchTrackId}/manual-match`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const data = await resp.json();
    if (resp.ok) {
      showToast(data.message || 'Download initiated!', 'info');
      hideManualMatchModal();
      const artistId = window._currentArtistId;
      if (artistId) loadArtistDetailPage(artistId);
    } else {
      showToast(data.error || 'Failed to initiate download', 'error');
    }
  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
  } finally {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.innerHTML = '<i class="fas fa-download me-1"></i>Fetch & Replace';
    }
  }
}

// ----- AcoustID: identify + caution resolution -----
let _identifyResults = [];

async function identifyTrackFile() {
  const trackId = _currentManualMatchTrackId;
  if (!trackId) return;
  const resultsEl = document.getElementById('manualMatchResults');
  const btn = document.getElementById('manualMatchIdentifyBtn');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Fingerprinting...';
  }
  if (resultsEl) resultsEl.innerHTML = '<div class="text-secondary small py-2"><i class="fas fa-spinner fa-spin me-1"></i>Fingerprinting and looking up AcoustID...</div>';
  try {
    const resp = await fetch(`/api/track/${trackId}/identify`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const data = await resp.json();
    if (!resp.ok) {
      if (resultsEl) resultsEl.innerHTML = `<div class="text-warning small py-2">${escapeHtml(data.error || 'Identify failed')}</div>`;
      return;
    }
    _identifyResults = data.candidates || [];
    if (data.auto_apply) {
      // strong single match -> auto-apply
      await applyIdentifyCandidate(data.auto_apply);
      return;
    }
    if (_identifyResults.length === 0) {
      if (resultsEl) resultsEl.innerHTML = `<div class="text-secondary small py-2">${escapeHtml(data.message || 'No match found')}</div>`;
      return;
    }
    if (resultsEl) {
      resultsEl.innerHTML = '<div class="small text-secondary py-1">Pick the match to apply:</div>' +
        _identifyResults.map((c, i) => {
          const artists = (c.artists || []).join(', ') || '?';
          return `<div class="d-flex justify-content-between align-items-center border-bottom border-secondary border-opacity-25 py-1">
            <div class="text-truncate"><span class="badge bg-secondary me-1" style="font-size:0.65rem;">${Math.round(c.score * 100)}%</span>
              <span class="fw-semibold">${escapeHtml(c.title || '?')}</span>
              <span class="text-secondary small"> — ${escapeHtml(artists)}</span></div>
            <button class="btn btn-sm btn-outline-success btn-icon ms-2" title="Apply this match" onclick="applyIdentifyCandidate(_identifyResults[${i}])"><i class="fas fa-check"></i></button>
          </div>`;
        }).join('');
    }
  } catch (err) {
    if (resultsEl) resultsEl.innerHTML = `<div class="text-danger small py-2">Network error: ${escapeHtml(err.message)}</div>`;
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '<i class="fas fa-fingerprint me-1"></i>Identify this file';
    }
  }
}

async function applyIdentifyCandidate(candidate) {
  const trackId = _currentManualMatchTrackId;
  if (!trackId || !candidate) return;
  try {
    const resp = await fetch(`/api/track/${trackId}/identify`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ candidate }),
    });
    const data = await resp.json();
    const resultsEl = document.getElementById('manualMatchResults');
    if (resp.ok) {
      if (resultsEl) resultsEl.innerHTML = `<div class="text-success small py-2">${escapeHtml(data.message || 'Applied')}</div>`;
      showToast(data.message || 'Match applied', 'success');
      const artistId = window._currentArtistId;
      if (artistId) loadArtistDetailPage(artistId);
    } else {
      if (resultsEl) resultsEl.innerHTML = `<div class="text-danger small py-2">${escapeHtml(data.error || 'Apply failed')}</div>`;
    }
  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
  }
}

async function resolveCaution(trackId, action) {
  try {
    const resp = await fetch(`/api/track/${trackId}/caution`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action }),
    });
    const data = await resp.json();
    if (resp.ok) {
      showToast(data.message, 'success');
      const artistId = window._currentArtistId;
      if (artistId) loadArtistDetailPage(artistId);
    } else {
      showToast(data.error || 'Action failed', 'error');
    }
  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
  }
}

// ----- Track & Album Action Handlers -----
async function toggleTrackMonitor(trackId) {
  try {
    const resp = await fetch(`/api/track/${trackId}/toggle-monitor`, { method: 'POST' });
    const data = await resp.json();
    if (resp.ok) {
      showToast(data.message, 'info');
      const artistId = window._currentArtistId;
      if (artistId) loadArtistDetailPage(artistId);
    } else {
      showToast(data.error || 'Failed to update track', 'error');
    }
  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
  }
}

async function toggleAlbumMonitor(albumId) {
  try {
    const resp = await fetch(`/api/album/${albumId}/toggle-monitor`, { method: 'POST' });
    const data = await resp.json();
    if (resp.ok) {
      showToast(data.message, 'info');
      const artistId = window._currentArtistId;
      if (artistId) loadArtistDetailPage(artistId);
    } else {
      showToast(data.error || 'Failed to update album', 'error');
    }
  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
  }
}

async function downloadTrack(trackId) {
  try {
    const resp = await fetch(`/api/track/${trackId}/download`, { method: 'POST' });
    const data = await resp.json();
    if (resp.ok) {
      showToast('Track queued for download', 'info');
    } else {
      showToast(data.error || 'Failed to queue', 'error');
    }
  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
  }
}

async function cancelTrack(trackId) {
  try {
    const resp = await fetch(`/api/track/${trackId}/cancel`, { method: 'POST' });
    const data = await resp.json();
    showToast(data.message, 'info');
  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
  }
}

function deleteTrack(trackId, title) {
  showConfirmModal(
    'Delete Track',
    `Delete downloaded audio file for "<strong>${escapeHtml(title)}</strong>"?`,
    async () => {
      hideConfirmModal();
      try {
        const resp = await fetch(`/api/track/${trackId}?delete_files=true`, { method: 'DELETE' });
        const data = await resp.json();
        showToast(data.message, 'success');
        const artistId = window._currentArtistId;
        if (artistId) loadArtistDetailPage(artistId);
      } catch (err) {
        showToast('Network error: ' + err.message, 'error');
      }
    },
    'Delete File',
    'btn-danger'
  );
}

async function downloadAlbum(albumId) {
  try {
    const resp = await fetch(`/api/album/${albumId}/download`, { method: 'POST' });
    const data = await resp.json();
    showToast(data.message, 'info');
  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
  }
}

function confirmDeleteAlbum(albumId, albumName) {
  showConfirmModal(
    'Delete Album',
    `Delete downloaded tracks for album "<strong>${escapeHtml(albumName)}</strong>"?`,
    async () => {
      hideConfirmModal();
      try {
        const resp = await fetch(`/api/album/${albumId}?delete_files=true`, { method: 'DELETE' });
        const data = await resp.json();
        showToast(data.message, 'success');
        const artistId = window._currentArtistId;
        if (artistId) loadArtistDetailPage(artistId);
      } catch (err) {
        showToast('Network error: ' + err.message, 'error');
      }
    },
    'Delete Album',
    'btn-danger'
  );
}

async function downloadArtistMissing(artistId) {
  try {
    const resp = await fetch(`/api/artist/${artistId}/download-missing`, { method: 'POST' });
    const data = await resp.json();
    showToast(data.message, 'info');
  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
  }
}

async function syncArtistDiscography(artistId) {
  try {
    const resp = await fetch(`/api/artist/${artistId}/sync`, { method: 'POST' });
    const data = await resp.json();
    showToast(data.message, 'info');
  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
  }
}

function confirmDeleteArtist(artistId, artistName) {
  showConfirmModal(
    'Remove Artist',
    `Are you sure you want to remove artist "<strong>${escapeHtml(artistName)}</strong>"?<br><br>
     <div class="form-check">
       <input class="form-check-input" type="checkbox" id="delArtistFilesCheck">
       <label class="form-check-label" for="delArtistFilesCheck">Also delete downloaded audio files from /music</label>
     </div>`,
    async () => {
      const delFiles = document.getElementById('delArtistFilesCheck')?.checked || false;
      hideConfirmModal();
      try {
        const resp = await fetch(`/api/artist/${artistId}?delete_files=${delFiles}`, { method: 'DELETE' });
        const data = await resp.json();
        showToast(data.message, 'success');
        window.location.href = '/';
      } catch (err) {
        showToast('Network error: ' + err.message, 'error');
      }
    },
    'Delete Artist',
    'btn-danger'
  );
}

// ----- Interactive Root Folder Importer (/import) -----
let _importCandidates = [];
let _selectedImportFolders = new Set();
let _bulkImportRunning = false;
let _selectedImportFolder = null;
let _selectArtistSearchDebounce = null;

function _folderRowId(folderName) {
  return 'importRow_' + escapeHtml(folderName).replace(/[^a-zA-Z0-9]/g, '_');
}

async function loadImportCandidates() {
  const tbody = document.getElementById('importCandidatesTbody');
  const loading = document.getElementById('importLoading');
  if (!tbody) return;

  if (loading) loading.classList.remove('d-none');
  tbody.innerHTML = `<tr><td colspan="6" class="text-center text-secondary py-4"><div class="spinner-border spinner-border-sm text-danger me-2" role="status"></div>Scanning /music directory...</td></tr>`;
  try {
    const [candResp, statusResp] = await Promise.all([
      fetch('/api/import/candidates'),
      fetch('/api/import/bulk/status'),
    ]);
    const candidates = await candResp.json();
    const status = statusResp.ok ? await statusResp.json() : null;
    _importCandidates = candidates;
    if (status && status.active) {
      _bulkImportRunning = true;
    }
    renderImportCandidates(candidates);
    updateBulkImportBar();
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="6" class="text-danger py-3">Failed to scan: ${escapeHtml(err.message)}</td></tr>`;
  } finally {
    if (loading) loading.classList.add('d-none');
  }
}

function updateBulkImportBar() {
  const bar = document.getElementById('bulkImportBar');
  const btn = document.getElementById('importSelectedBtn');
  const countEl = document.getElementById('importSelectedCount');
  if (!bar) return;

  const readyCount = _importCandidates.filter(c => !c.is_already_imported).length;
  const selCount = _selectedImportFolders.size;

  if (bar) bar.classList.toggle('d-none', !(selCount > 0 || _bulkImportRunning));
  if (btn) {
    btn.disabled = _bulkImportRunning || selCount === 0;
    btn.innerHTML = _bulkImportRunning
      ? '<i class="fas fa-spinner fa-spin me-1"></i>Importing...'
      : '<i class="fas fa-file-import me-1"></i>Import Selected (<span id="importSelectedCount">' + selCount + '</span>)';
  }
  if (countEl && !_bulkImportRunning) countEl.textContent = selCount;
  const selectAll = document.getElementById('selectAllFolders');
  if (selectAll) {
    selectAll.checked = readyCount > 0 && selCount === readyCount;
    selectAll.indeterminate = selCount > 0 && selCount < readyCount;
  }
}

function toggleSelectAllFolders(el) {
  _selectedImportFolders.clear();
  if (el && el.checked) {
    for (let i = 0; i < _importCandidates.length; i++) {
      const c = _importCandidates[i];
      if (!c.is_already_imported) _selectedImportFolders.add(c.folder_name);
    }
  }
  renderImportCandidates(_importCandidates);
  updateBulkImportBar();
}

function onFolderSelectChange(idx, checked) {
  const c = _importCandidates[idx];
  if (!c) return;
  if (checked) _selectedImportFolders.add(c.folder_name);
  else _selectedImportFolders.delete(c.folder_name);
  const selectAll = document.getElementById('selectAllFolders');
  if (selectAll) selectAll.checked = false;
  updateBulkImportBar();
}

function clearImportSelection() {
  _selectedImportFolders.clear();
  renderImportCandidates(_importCandidates);
  updateBulkImportBar();
}

function renderImportCandidates(candidates) {
  const tbody = document.getElementById('importCandidatesTbody');
  if (!tbody) return;

  if (!Array.isArray(candidates) || candidates.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="text-center text-secondary py-4">No audio folders found in /music</td></tr>';
    return;
  }

  let html = '';
  for (let i = 0; i < candidates.length; i++) {
    const c = candidates[i];
    const isImported = c.is_already_imported;
    const suggested = c.suggested_deezer;
    const rowId = _folderRowId(c.folder_name);

    html += `
      <tr id="${rowId}">
        <td>
          <input type="checkbox" class="form-check-input folder-select" data-idx="${i}" ${isImported ? 'disabled' : ''} ${_selectedImportFolders.has(c.folder_name) ? 'checked' : ''} onchange="onFolderSelectChange(${i}, this.checked)">
        </td>
        <td class="fw-bold">
          <div class="d-flex align-items-center gap-2">
            <i class="fas fa-folder text-warning"></i>
            <span>${escapeHtml(c.folder_name)}</span>
          </div>
        </td>
        <td>
          <div class="d-flex align-items-center justify-content-between gap-2">
            <div class="d-flex align-items-center gap-2">
              ${suggested && suggested.image_url ? `<img src="${suggested.image_url}" class="rounded-circle" width="30" height="30" style="object-fit: cover;">` : '<div class="rounded-circle bg-secondary d-inline-flex align-items-center justify-content-center text-dark" style="width:30px;height:30px;"><i class="fas fa-user small"></i></div>'}
              <div>
                <div class="fw-semibold">${suggested ? escapeHtml(suggested.name) : `<span class="text-secondary">${escapeHtml(c.detected_artist || c.folder_name)}</span>`}</div>
                ${suggested ? `<small class="text-secondary" style="font-size:0.75rem;">Deezer ID: ${suggested.id} · ${suggested.nb_album || 0} releases</small>` : '<small class="text-warning" style="font-size:0.75rem;">Unmatched Deezer Profile</small>'}
              </div>
            </div>
            ${!isImported ? `
              <button class="btn btn-outline-secondary btn-sm py-0 px-2" style="font-size:0.75rem;" title="Change Deezer artist match" onclick="openSelectArtistModal(${i})">
                <i class="fas fa-search me-1"></i>Change
              </button>` : ''
            }
          </div>
        </td>
        <td>${c.album_count} albums / ${c.track_count} tracks</td>
        <td class="import-row-status">
          ${isImported
            ? '<span class="badge bg-success-subtle text-success"><i class="fas fa-check-circle me-1"></i>Managed</span>'
            : '<span class="badge bg-secondary-subtle text-secondary"><i class="fas fa-arrow-circle-right me-1"></i>Ready</span>'
          }
        </td>
        <td class="text-end">
          ${!isImported ? `
            <button class="btn btn-brand btn-sm" id="btn_import_${escapeHtml(c.folder_name).replace(/[^a-zA-Z0-9]/g, '_')}" onclick="importArtistFolder(${i}, this)">
              <i class="fas fa-file-import me-1"></i>Import
            </button>` : `<a href="/artist/${c.existing_artist_id}" class="btn btn-outline-secondary btn-sm"><i class="fas fa-external-link-alt me-1"></i>View</a>`
          }
        </td>
      </tr>`;
  }
  tbody.innerHTML = html;
  updateBulkImportBar();
}

function _setImportRowStatus(folderName, html) {
  const row = document.getElementById(_folderRowId(folderName));
  if (!row) return;
  const cell = row.querySelector('.import-row-status');
  if (cell) cell.innerHTML = html;
}

function _importItemFor(folderName) {
  const cand = _importCandidates.find(c => c.folder_name === folderName);
  const suggested = cand ? cand.suggested_deezer : null;
  return {
    folder_name: folderName,
    deezer_id: suggested && suggested.id ? suggested.id : null,
  };
}

async function _postBulkImport(items, onQueued) {
  try {
    const resp = await fetch('/api/import/folder/bulk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items }),
    });
    const data = await resp.json();
    if (resp.ok) {
      _bulkImportRunning = true;
      updateBulkImportBar();
      if (onQueued) onQueued();
    } else {
      showToast(data.error || 'Could not start import', 'error');
    }
  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
  }
}

async function importSelectedFolders() {
  if (_bulkImportRunning) return;
  const folders = Array.from(_selectedImportFolders);
  if (folders.length === 0) {
    showToast('Select at least one folder to import', 'error');
    return;
  }
  const items = folders.map(f => _importItemFor(f));
  await _postBulkImport(items, () => {
    showToast(`Queued import for ${items.length} folder(s). This runs in the background — you can keep browsing.`, 'info');
    for (const f of folders) {
      _setImportRowStatus(f, '<span class="badge bg-info-subtle text-info"><i class="fas fa-hourglass-half me-1"></i>Queued</span>');
    }
  });
}

async function importArtistFolder(idx, btnEl) {
  if (_bulkImportRunning) {
    showToast('An import batch is already running — please wait for it to finish.', 'error');
    return;
  }
  const cand = _importCandidates[idx];
  if (!cand) return;
  const folderName = cand.folder_name;
  if (btnEl) {
    btnEl.disabled = true;
    btnEl.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Queued...';
  }
  const item = _importItemFor(folderName);
  if (cand.suggested_deezer && cand.suggested_deezer.id) item.deezer_id = cand.suggested_deezer.id;
  await _postBulkImport([item], () => {
    showToast(`Import queued for '${folderName}' in the background.`, 'info');
    _setImportRowStatus(folderName, '<span class="badge bg-info-subtle text-info"><i class="fas fa-hourglass-half me-1"></i>Queued</span>');
  });
}

// SocketIO progress handler (registered in the socket block below)
function handleImportProgress(data) {
  if (!data || !data.status) return;
  const bar = document.getElementById('bulkProgressBar');
  const text = document.getElementById('bulkProgressText');
  const total = data.total || 0;

  if (data.status === 'finished') {
    _bulkImportRunning = false;
    updateBulkImportBar();
    if (bar) bar.style.width = '100%';
    if (text) text.textContent = `Finished: ${data.done || 0} imported, ${data.failed || 0} failed`;
    const msg = data.failed > 0
      ? `Bulk import finished: ${data.done} imported, ${data.failed} failed.`
      : `Bulk import finished: ${data.done} artist(s) imported successfully!`;
    showToast(msg, data.failed > 0 ? 'warning' : 'success');
    setTimeout(() => {
      if (bar) bar.style.width = '0%';
      if (text) text.textContent = 'Ready to import';
    }, 4000);
    _selectedImportFolders.clear();
    loadImportCandidates();
    return;
  }

  if (data.status === 'importing') {
    _bulkImportRunning = true;
    updateBulkImportBar();
    const pct = total > 0 ? Math.round(((data.index || 0) / total) * 100) : 0;
    if (bar) bar.style.width = pct + '%';
    if (text) text.textContent = `Importing (${(data.index || 0) + 1}/${total}): ${data.folder_name || ''}...`;
    if (data.folder_name) {
      _setImportRowStatus(data.folder_name, '<span class="badge bg-warning-subtle text-warning"><i class="fas fa-spinner fa-spin me-1"></i>Importing...</span>');
    }
  } else if (data.status === 'done') {
    const pct = total > 0 ? Math.round((((data.index || 0) + 1) / total) * 100) : 0;
    if (bar) bar.style.width = pct + '%';
    if (text && data.folder_name) text.textContent = `${data.folder_name} → ${data.artist_name || ''} (${data.matched_tracks || 0} tracks mapped)`;
    if (data.folder_name) {
      _setImportRowStatus(data.folder_name, '<span class="badge bg-success-subtle text-success"><i class="fas fa-check me-1"></i>Imported</span>');
    }
  } else if (data.status === 'error') {
    if (text && data.folder_name) text.textContent = `${data.folder_name} failed: ${data.error || ''}`;
    if (data.folder_name) {
      _setImportRowStatus(data.folder_name, `<span class="badge bg-danger-subtle text-danger" title="${escapeHtml(data.error || '')}"><i class="fas fa-times me-1"></i>Failed</span>`);
    }
  }
}

function openSelectArtistModal(idx) {
  const cand = _importCandidates[idx];
  if (!cand) return;
  const folderName = cand.folder_name;
  _selectedImportFolder = folderName;
  const modal = document.getElementById('selectArtistModal');
  const span = document.getElementById('selectArtistFolderSpan');
  const input = document.getElementById('selectArtistSearchInput');
  const resultsContainer = document.getElementById('selectArtistResults');

  if (!modal) return;

  if (span) span.textContent = folderName;
  if (input) {
    input.value = folderName;
  }

  // Preload alternate matches if available
  if (Array.isArray(cand.alternate_matches) && cand.alternate_matches.length > 0) {
    renderSelectArtistResults(cand.alternate_matches);
  } else {
    performSelectArtistSearch(folderName);
  }

  modal.classList.remove('d-none');
  if (input) {
    input.focus();
    input.oninput = () => {
      clearTimeout(_selectArtistSearchDebounce);
      _selectArtistSearchDebounce = setTimeout(() => {
        performSelectArtistSearch(input.value.trim());
      }, 300);
    };
  }
}

function hideSelectArtistModal() {
  const modal = document.getElementById('selectArtistModal');
  if (modal) modal.classList.add('d-none');
}

async function performSelectArtistSearch(query) {
  const resultsContainer = document.getElementById('selectArtistResults');
  const spinner = document.getElementById('selectArtistSpinner');
  if (!resultsContainer) return;

  if (!query) {
    resultsContainer.innerHTML = '<div class="text-secondary text-center py-3">Type an artist name to search Deezer</div>';
    return;
  }

  if (spinner) spinner.classList.remove('d-none');
  try {
    const resp = await fetch(`/api/search-artist?q=${encodeURIComponent(query)}`);
    const results = await resp.json();
    renderSelectArtistResults(results);
  } catch (err) {
    resultsContainer.innerHTML = `<div class="text-danger small py-2">Search failed: ${escapeHtml(err.message)}</div>`;
  } finally {
    if (spinner) spinner.classList.add('d-none');
  }
}

function renderSelectArtistResults(results) {
  const container = document.getElementById('selectArtistResults');
  if (!container) return;

  if (!Array.isArray(results) || results.length === 0) {
    container.innerHTML = '<div class="text-secondary text-center py-3">No artists found on Deezer</div>';
    return;
  }

  let html = '';
  for (const a of results) {
    html += `
      <div class="d-flex align-items-center justify-content-between p-2 rounded bg-dark border border-secondary border-opacity-25 hover-bg-dark">
        <div class="d-flex align-items-center gap-3">
          ${a.image_url ? `<img src="${a.image_url}" class="rounded-circle" width="38" height="38" style="object-fit: cover;">` : '<div class="rounded-circle bg-secondary d-flex align-items-center justify-content-center" style="width:38px;height:38px;"><i class="fas fa-user"></i></div>'}
          <div>
            <div class="fw-bold">${escapeHtml(a.name)}</div>
            <small class="text-secondary">${a.nb_album || 0} releases · ${(a.nb_fan || 0).toLocaleString()} fans</small>
          </div>
        </div>
        <button type="button" class="btn btn-sm btn-brand" onclick="confirmSelectArtistForFolder(${a.id}, '${escapeHtml(a.name).replace(/'/g, "\\'")}', '${escapeHtml(a.image_url || '').replace(/'/g, "\\'")}', ${a.nb_album || 0})">
          <i class="fas fa-check me-1"></i>Select
        </button>
      </div>`;
  }
  container.innerHTML = html;
}

function confirmSelectArtistForFolder(deezerId, name, imageUrl, nbAlbums) {
  if (!_selectedImportFolder) return;

  const cand = _importCandidates.find(c => c.folder_name === _selectedImportFolder);
  if (cand) {
    cand.suggested_deezer = {
      id: deezerId,
      name: name,
      image_url: imageUrl,
      nb_album: nbAlbums,
    };
  }

  hideSelectArtistModal();
  renderImportCandidates(_importCandidates);
  showToast(`Matched '${_selectedImportFolder}' to Deezer artist '${name}'`, 'info');
}

// ----- Artist Discography Filters Modal -----
let _artistFiltersId = null;

async function openArtistFiltersModal(artistId) {
  try {
    const resp = await fetch(`/api/artist/${artistId}`);
    if (!resp.ok) {
      showToast('Could not load artist preferences', 'error');
      return;
    }
    const a = await resp.json();
    _artistFiltersId = artistId;
    document.getElementById('artistFiltersName').textContent = a.name;
    document.getElementById('artistFilterRemixes').checked = a.filter_remixes !== false;
    document.getElementById('artistFilterLofi').checked = a.filter_lofi !== false;
    document.getElementById('artistFilterLive').checked = a.filter_live !== false;
    document.getElementById('artistFilterCompilations').checked = a.filter_compilations !== false;
    document.getElementById('artistIncAlbums').checked = a.include_albums !== false;
    document.getElementById('artistIncSingles').checked = a.include_singles !== false;
    document.getElementById('artistIncCompilations').checked = a.include_compilations === true;
    document.getElementById('artistOptMonitored').checked = a.monitored !== false;
    document.getElementById('artistOptAutoDownload').checked = a.auto_download === true;
    const modal = document.getElementById('artistFiltersModal');
    if (modal) modal.classList.remove('d-none');
  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
  }
}

function closeArtistFiltersModal() {
  const modal = document.getElementById('artistFiltersModal');
  if (modal) modal.classList.add('d-none');
  _artistFiltersId = null;
}

async function saveArtistFilters() {
  if (!_artistFiltersId) return;
  const artistId = _artistFiltersId;
  const btn = document.getElementById('artistFiltersSaveBtn');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Saving...';
  }
  const payload = {
    filter_remixes: document.getElementById('artistFilterRemixes').checked,
    filter_lofi: document.getElementById('artistFilterLofi').checked,
    filter_live: document.getElementById('artistFilterLive').checked,
    filter_compilations: document.getElementById('artistFilterCompilations').checked,
    include_albums: document.getElementById('artistIncAlbums').checked,
    include_singles: document.getElementById('artistIncSingles').checked,
    include_compilations: document.getElementById('artistIncCompilations').checked,
    monitored: document.getElementById('artistOptMonitored').checked,
    auto_download: document.getElementById('artistOptAutoDownload').checked,
  };
  try {
    const resp = await fetch(`/api/artist/${artistId}/monitor`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok) {
      showToast(data.error || 'Failed to save filters', 'error');
    } else {
      showToast('Filters saved. Re-syncing discography...', 'info');
      // Re-sync so excluded/included releases take effect immediately
      await fetch(`/api/artist/${artistId}/sync`, { method: 'POST' });
    }
  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
  } finally {
    closeArtistFiltersModal();
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '<i class="fas fa-check me-1"></i>Save & Re-sync';
    }
    if (window._currentArtistId === artistId) {
      loadArtistDetailPage(artistId);
    }
  }
}

// ----- Queue & Activity Page (/queue) -----
async function loadQueuePage() {
  const activeContainer = document.getElementById('queueActiveList');
  const historyTbody = document.getElementById('queueHistoryTbody');
  if (!activeContainer || !historyTbody) return;

  try {
    const resp = await fetch('/api/queue');
    const data = await resp.json();

    // Active
    if (!data.active || data.active.length === 0) {
      activeContainer.innerHTML = '<div class="text-secondary p-3 text-center">No downloads currently in queue</div>';
    } else {
      let html = '';
      for (const j of data.active) {
        html += `
          <div class="card bg-dark-card border-0 mb-2 p-3" id="queue_job_${j.id}">
            <div class="d-flex align-items-center justify-content-between">
              <div>
                <div class="fw-bold">${escapeHtml(j.artist_name)} — ${escapeHtml(j.title)}</div>
                <div class="text-secondary small">${escapeHtml(j.album_name || '')}</div>
              </div>
              <div class="d-flex align-items-center gap-2">
                ${statusBadgeHtml(j.status)}
                <button class="btn btn-sm btn-outline-danger btn-icon" onclick="cancelQueueJob(${j.id})"><i class="fas fa-ban"></i></button>
              </div>
            </div>
            <div class="progress mt-2" style="height: 4px;">
              <div class="progress-bar bg-info ${j.status === 'downloading' ? 'progress-bar-striped progress-bar-animated' : ''}" style="width: ${j.status === 'downloading' ? Math.max(5, j.progress || 0) : 0}%"></div>
            </div>
          </div>`;
      }
      activeContainer.innerHTML = html;
    }

    // History
    if (!data.history || data.history.length === 0) {
      historyTbody.innerHTML = '<tr><td colspan="5" class="text-center text-secondary py-3">No history</td></tr>';
    } else {
      let html = '';
      for (const j of data.history) {
        html += `
          <tr>
            <td>${escapeHtml(j.artist_name)}</td>
            <td>${escapeHtml(j.title)}</td>
            <td>${statusBadgeHtml(j.status, j.error_message)}</td>
            <td class="text-secondary small">${j.created_at ? new Date(j.created_at).toLocaleTimeString() : '—'}</td>
            <td class="text-end">
              ${j.status === 'failed' || j.status === 'cancelled' ? `
                <button class="btn btn-sm btn-outline-warning btn-icon" onclick="retryQueueJob(${j.id})"><i class="fas fa-redo"></i></button>` : ''
              }
            </td>
          </tr>`;
      }
      historyTbody.innerHTML = html;
    }
  } catch (err) {
    console.error('Failed to load queue:', err);
  }
}

async function retryQueueJob(jobId) {
  try {
    const resp = await fetch(`/api/jobs/${jobId}/retry`, { method: 'POST' });
    const data = await resp.json();
    showToast(data.message, 'success');
    loadQueuePage();
  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
  }
}

async function cancelQueueJob(jobId) {
  try {
    const resp = await fetch(`/api/jobs/${jobId}/cancel`, { method: 'POST' });
    const data = await resp.json();
    showToast(data.message, 'info');
    loadQueuePage();
  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
  }
}

async function retryAllFailedJobs() {
  try {
    const resp = await fetch('/api/queue/retry-failed', { method: 'POST' });
    const data = await resp.json();
    showToast(data.message, 'success');
    loadQueuePage();
  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
  }
}

// ----- Socket.IO Real-Time Handlers -----
socket.on('connect', () => {
  console.log('[SOCKET] Connected to fnack backend');
});

socket.on('download_progress', (data) => {
  // Update track row if viewing artist page
  if (data.track_id) {
    const statusCell = document.getElementById(`track_status_${data.track_id}`);
    if (statusCell) {
      statusCell.innerHTML = statusBadgeHtml(data.status, data.error_message);
    }
    const row = document.getElementById(`track_row_${data.track_id}`);
    if (row && data.status === 'completed') {
      // Reload artist page slightly debounced
      clearTimeout(window._detailReloadDebounce);
      window._detailReloadDebounce = setTimeout(() => {
        if (window._currentArtistId) loadArtistDetailPage(window._currentArtistId);
      }, 1000);
    }
  }

  // Reload queue if viewing queue page
  if (document.getElementById('queueActiveList')) {
    clearTimeout(window._queueReloadDebounce);
    window._queueReloadDebounce = setTimeout(loadQueuePage, 500);
  }
});

let _dashboardReloadDebounce = null;
function debouncedLoadDashboard(delay = 2000) {
  // Skip full-grid reloads entirely when the dashboard is not visible or the
  // tab is hidden; the next visible load refreshes everything anyway.
  if (document.hidden) return;
  if (!document.getElementById('artistsDashboardGrid')) return;
  clearTimeout(_dashboardReloadDebounce);
  _dashboardReloadDebounce = setTimeout(() => {
    loadDashboardArtists();
  }, delay);
}

socket.on('artist_updated', (data) => {
  debouncedLoadDashboard(2000);
  if (window._currentArtistId && data && data.artist_id === window._currentArtistId) {
    loadArtistDetailPage(window._currentArtistId);
  }
});

socket.on('artist_added', () => {
  debouncedLoadDashboard(500);
});

socket.on('artist_synced', (data) => {
  debouncedLoadDashboard(500);
  if (window._currentArtistId && data && data.artist_id === window._currentArtistId) {
    loadArtistDetailPage(window._currentArtistId);
  }
});

socket.on('artist_deleted', () => {
  debouncedLoadDashboard(300);
});

socket.on('toast', (data) => {
  showToast(data.message, data.type || 'info');
});

socket.on('import_progress', (data) => {
  if (typeof handleImportProgress === 'function') handleImportProgress(data);
});

// ----- DOM Ready Initializer -----
document.addEventListener('DOMContentLoaded', () => {
  initArtistSearch();
  initLibraryFilter();

  if (document.getElementById('artistsDashboardGrid')) {
    loadDashboardArtists();
  }

  if (document.getElementById('artistPageContainer')) {
    const pathParts = window.location.pathname.split('/');
    const artistId = parseInt(pathParts[pathParts.length - 1]);
    if (!isNaN(artistId)) {
      window._currentArtistId = artistId;
      loadArtistDetailPage(artistId);
    }
  }

  if (document.getElementById('importCandidatesTbody')) {
    loadImportCandidates();
  }

  if (document.getElementById('queueActiveList')) {
    loadQueuePage();
  }

  // Wire modal events
  const okBtn = document.getElementById('confirmModalOkBtn');
  if (okBtn) {
    okBtn.addEventListener('click', () => {
      if (_customModalCallback) _customModalCallback();
    });
  }

  const cancelBtn = document.getElementById('confirmModalCancelBtn');
  if (cancelBtn) {
    cancelBtn.addEventListener('click', hideConfirmModal);
  }

  const addConfirm = document.getElementById('addArtistConfirmBtn');
  if (addConfirm) {
    addConfirm.addEventListener('click', confirmAddArtist);
  }

  const addCancel = document.getElementById('addArtistCancelBtn');
  if (addCancel) {
    addCancel.addEventListener('click', hideAddArtistModal);
  }
});
// ============================================================
// Plugins page (Settings → Plugins equivalent, top-level /plugins)
// ============================================================

const PLUGIN_TYPE_LABELS = {
  downloader: 'Downloaders',
  metadata_provider: 'Metadata Providers',
  lyrics_provider: 'Lyrics Providers',
  fingerprint: 'Fingerprinting',
  scan_trigger: 'Scan Triggers',
  library_task: 'Library Tasks',
  vpn: 'VPN',
  storage_backend: 'Storage Backends',
  server_extension: 'Server Extensions',
  ui_extension: 'UI Extensions',
  event_hook: 'Event Hooks',
  auth_provider: 'Auth Providers',
  library_source: 'Library Sources',
  conflict_resolver: 'Conflict Resolvers',
  recommendation: 'Recommendations',
};

const PRIORITY_TYPES = new Set(['downloader', 'metadata_provider', 'lyrics_provider']);

function pluginTrustBadge(trust) {
  if (trust === 'official') return '<span class="badge bg-success-subtle text-success">Official</span>';
  if (trust === 'verified') return '<span class="badge bg-info-subtle text-info">Verified</span>';
  return '<span class="badge bg-secondary-subtle text-secondary">Community</span>';
}

async function loadPluginsPage() {
  const container = document.getElementById('pluginsPageContainer');
  if (!container) return;
  try {
    const resp = await fetch('/api/plugins');
    const plugins = await resp.json();
    const healthMap = {};
    await Promise.all(plugins.map(async (p) => {
      try {
        const hr = await fetch(`/api/plugins/${encodeURIComponent(p.id)}/health`);
        if (hr.ok) healthMap[p.id] = await hr.json();
      } catch (e) { /* no health row yet */ }
    }));

    // Group by type (a plugin with multiple types appears once per type)
    const grouped = {};
    for (const p of plugins) {
      for (const t of p.type || []) {
        if (!grouped[t]) grouped[t] = [];
        grouped[t].push({ ...p, health: healthMap[p.id] });
      }
    }

    const typeOrder = ['downloader', 'metadata_provider', 'fingerprint', 'scan_trigger',
      'library_task', 'vpn', 'event_hook', 'server_extension', 'ui_extension',
      'lyrics_provider', 'storage_backend', 'auth_provider', 'library_source',
      'conflict_resolver', 'recommendation'];

    let html = '';
    for (const t of typeOrder) {
      const items = grouped[t];
      if (!items || !items.length) continue;
      html += `<div class="card bg-dark-card border-0 shadow-sm mb-4">
        <div class="card-body p-4">
          <div class="d-flex align-items-center justify-content-between mb-2">
            <h5 class="fw-bold m-0"><i class="fas fa-cubes text-danger me-2"></i>${PLUGIN_TYPE_LABELS[t] || t}</h5>
            <span class="badge bg-secondary-subtle text-secondary">${items.length}</span>
          </div>
          <div class="table-responsive">
            <table class="table table-dark table-hover m-0 align-middle">
              <thead class="table-secondary text-secondary small text-uppercase" style="letter-spacing: 0.5px;">
                <tr><th>Plugin</th><th>Version</th><th>Trust</th><th>Status</th><th>Priority</th><th class="text-end">Actions</th></tr>
              </thead>
              <tbody>`;
      for (const p of items) {
        const h = p.health || {};
        const status = p.enabled
          ? `<span class="badge bg-success-subtle text-success">Enabled</span>`
          : `<span class="badge bg-secondary-subtle text-secondary">Disabled</span>`;
        const healthBadge = h.last_error
          ? `<div class="small text-danger mt-1" title="${escapeHtml(h.last_error)}">⚠ ${h.consecutive_failures || 0} failures</div>`
          : (p.consecutive_failures ? `<div class="small text-warning mt-1">${p.consecutive_failures} recent failures</div>` : '');
        const prioInput = PRIORITY_TYPES.has(t)
          ? `<input type="number" min="1" class="form-control form-control-sm plugin-priority" style="width: 80px;"
               data-plugin="${escapeHtml(p.id)}" value="${p.priority_override ?? p.priority}"
               title="Priority (lower = tried first); clears on empty" onchange="savePluginPriority('${escapeHtml(p.id)}', this.value)">`
          : `<span class="text-secondary small">${p.priority ?? '—'}</span>`;
        const hasSettings = (p.settings_schema || []).length > 0;
        const settingsBtn = hasSettings
          ? `<button class="btn btn-sm btn-outline-info btn-icon" title="Settings" onclick="openPluginSettings('${escapeHtml(p.id)}', '${escapeHtml(p.name)}')"><i class="fas fa-cog"></i></button>`
          : '';
        // Phase 3: non-bundled plugins get an Uninstall action (bundled ones
        // are disabled instead — auto-install would restore them on reboot).
        const isBundled = !!p.bundled;
        const uninstallBtn = isBundled ? '' : `<button class="btn btn-sm btn-outline-danger btn-icon" title="Uninstall" onclick="uninstallPlugin('${escapeHtml(p.id)}')"><i class="fas fa-trash"></i></button>`;
        html += `<tr>
          <td>
            <div class="fw-bold">${escapeHtml(p.name)}</div>
            <div class="text-secondary small font-monospace">${escapeHtml(p.id)}</div>
            ${healthBadge}
          </td>
          <td class="text-secondary small">${escapeHtml(p.version)}</td>
          <td>${pluginTrustBadge(p.trust_level)}</td>
          <td>${status}</td>
          <td>${prioInput}</td>
          <td class="text-end">
            ${settingsBtn}
            ${uninstallBtn}
            ${p.enabled
              ? `<button class="btn btn-sm btn-outline-danger btn-icon" title="Disable" onclick="setPluginEnabled('${escapeHtml(p.id)}', false)"><i class="fas fa-power-off"></i></button>`
              : `<button class="btn btn-sm btn-outline-success btn-icon" title="Enable" onclick="setPluginEnabled('${escapeHtml(p.id)}', true)"><i class="fas fa-power-off"></i></button>`}
          </td>
        </tr>`;
      }
      html += `</tbody></table></div></div></div>`;
    }

    if (!html) {
      html = '<div class="card bg-dark-card border-0 shadow-sm"><div class="card-body p-4 text-secondary text-center">No plugins installed. Bundled plugins appear here automatically.</div></div>';
    }
    container.innerHTML = html;
    // Keep the schema index for the settings modal (per-plugin settings UI).
    _pluginSchemas = {};
    for (const p of plugins) _pluginSchemas[p.id] = p.settings_schema || [];
  } catch (e) {
    container.innerHTML = `<div class="alert alert-danger">Error loading plugins: ${escapeHtml(e.message)}</div>`;
  }
}

let _pluginSchemas = {};

// Per-plugin settings modal — each plugin gets its own settings form rendered
// from its declared settings_schema (user requirement: no global settings).
async function openPluginSettings(pluginId, pluginName) {
  showConfirmModal(`Settings — ${escapeHtml(pluginName)}`,
    '<div class="text-secondary small py-2"><i class="fas fa-spinner fa-spin me-1"></i>Loading settings...</div>',
    async () => { await savePluginSettings(pluginId); },
    'Save', 'btn-brand');
  try {
    const [schema, current] = await Promise.all([
      Promise.resolve(_pluginSchemas[pluginId] || []),
      fetch(`/api/plugins/${encodeURIComponent(pluginId)}/settings`).then(r => r.json()).catch(() => ({})),
    ]);
    let form = '';
    for (const f of schema) {
      const key = f.key || '';
      const val = (current[key] !== undefined ? current[key] : (f.default !== undefined ? f.default : '')) ?? '';
      const label = escapeHtml(f.key || '');
      const req = f.required ? ' required' : '';
      if (f.type === 'boolean') {
        form += `<div class="form-check mb-3">
          <input class="form-check-input plugin-settings-field" type="checkbox" id="ps-${escapeHtml(key)}" data-key="${escapeHtml(key)}" ${val === true || val === 'true' ? 'checked' : ''}>
          <label class="form-check-label small" for="ps-${escapeHtml(key)}">${label}</label>
        </div>`;
      } else if (f.type === 'select') {
        const opts = (f.options || []).map(o =>
          `<option value="${escapeHtml(String(o))}" ${String(o) === String(val) ? 'selected' : ''}>${escapeHtml(String(o))}</option>`).join('');
        form += `<div class="mb-3"><label class="form-label small text-secondary">${label}</label>
          <select class="form-select plugin-settings-field" id="ps-${escapeHtml(key)}" data-key="${escapeHtml(key)}">${opts}</select></div>`;
      } else if (f.type === 'secret') {
        form += `<div class="mb-3"><label class="form-label small text-secondary">${label}</label>
          <input type="password" class="form-control plugin-settings-field" id="ps-${escapeHtml(key)}" data-key="${escapeHtml(key)}" value="${escapeHtml(String(val))}" placeholder="••••••••"${req}></div>`;
      } else {
        const inputType = f.type === 'number' ? 'number' : 'text';
        form += `<div class="mb-3"><label class="form-label small text-secondary">${label}</label>
          <input type="${inputType}" class="form-control plugin-settings-field" id="ps-${escapeHtml(key)}" data-key="${escapeHtml(key)}" value="${escapeHtml(String(val))}"${req}></div>`;
      }
    }
    if (!form) form = '<div class="text-secondary small">This plugin has no configurable settings.</div>';
    document.getElementById('confirmModalBody').innerHTML = form;
  } catch (e) {
    document.getElementById('confirmModalBody').innerHTML = `<div class="alert alert-danger">Error: ${escapeHtml(e.message)}</div>`;
  }
}

async function setPluginEnabled(pluginId, enabled) {
  try {
    const resp = await fetch(`/api/plugins/${encodeURIComponent(pluginId)}/${enabled ? 'enable' : 'disable'}`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) { showToast(data.error || 'Failed', 'danger'); return; }
    showToast(`${enabled ? 'Enabled' : 'Disabled'} ${pluginId}`, 'success');
    loadPluginsPage();
  } catch (e) {
    showToast('Network error: ' + e.message, 'danger');
  }
}

async function savePluginPriority(pluginId, value) {
  const priority = value === '' ? null : parseInt(value, 10);
  if (priority !== null && (isNaN(priority) || priority < 1)) {
    showToast('Priority must be a positive number', 'danger');
    loadPluginsPage();
    return;
  }
  try {
    const resp = await fetch(`/api/plugins/${encodeURIComponent(pluginId)}/priority`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ priority }),
    });
    const data = await resp.json();
    if (!resp.ok) { showToast(data.error || 'Failed', 'danger'); return; }
    showToast(`Priority updated for ${pluginId}`, 'success');
  } catch (e) {
    showToast('Network error: ' + e.message, 'danger');
  }
}

// Settings-tab forms contributed by plugins call this (e.g. fnack.navidrome).
async function savePluginSettings(pluginId) {
  const payload = {};
  // Generic per-plugin settings form (rendered from settings_schema).
  document.querySelectorAll(`#confirmModalBody .plugin-settings-field`).forEach((el) => {
    const key = el.dataset.key;
    if (!key) return;
    if (el.type === 'checkbox') payload[key] = el.checked ? 'true' : 'false';
    else payload[key] = el.value;
  });
  // Plugin-contributed settings_tab forms (e.g. fnack.navidrome).
  document.querySelectorAll(`[id^="plugin-${pluginId}-"]`).forEach((el) => {
    const key = el.id.replace(`plugin-${pluginId}-`, '');
    payload[key] = el.value;
  });
  try {
    const resp = await fetch(`/api/plugins/${encodeURIComponent(pluginId)}/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok) { showToast(data.error || 'Failed', 'danger'); return; }
    showToast('Plugin settings saved', 'success');
    hideConfirmModal();
    loadPluginsPage();
  } catch (e) {
    showToast('Network error: ' + e.message, 'danger');
  }
}

// ============================================================
// Phase 3: Marketplace + Repositories tabs
// ============================================================

function pluginCompatBadge(entry) {
  // entry: {min_core_version?, api_version?} — core is v0.2.x, api 1.0
  const ok = true; // the registry already validated on install; badge is informational
  return '<span class="badge bg-success-subtle text-success">Compatible</span>';
}

async function loadMarketplacePage() {
  const container = document.getElementById('marketplaceContainer');
  if (!container) return;
  try {
    const resp = await fetch('/api/plugins/marketplace');
    const plugins = await resp.json();
    const installed = await fetch('/api/plugins').then(r => r.json()).catch(() => []);
    const installedVersions = {};
    for (const p of installed) installedVersions[p.id] = p.version;

    if (!plugins.length) {
      container.innerHTML = '<div class="card bg-dark-card border-0 shadow-sm"><div class="card-body p-4 text-secondary text-center">No repositories added yet. Add one in the Repositories tab to browse plugins.</div></div>';
      return;
    }

    let html = '<div class="row g-3">';
    for (const e of plugins) {
      const installedV = installedVersions[e.id] || e.installed_version;
      const bundled = e.bundled;
      const hasUpdate = installedV && e.latest_version && installedV !== e.latest_version;
      let action = '';
      if (bundled) {
        action = '<span class="badge bg-secondary-subtle text-secondary">Installed (bundled)</span>';
      } else if (installedV && !hasUpdate) {
        action = `<span class="badge bg-success-subtle text-success">Installed v${escapeHtml(installedV)}</span>`;
      } else if (installedV && hasUpdate) {
        action = `<button class="btn btn-sm btn-brand" onclick="installPlugin('${escapeHtml(e.id)}', '${escapeHtml(e.latest_version)}')">Update to v${escapeHtml(e.latest_version)}</button>`;
      } else {
        action = `<button class="btn btn-sm btn-brand" onclick="installPlugin('${escapeHtml(e.id)}', '${escapeHtml(e.latest_version)}')">Install v${escapeHtml(e.latest_version)}</button>`;
      }
      const perms = (e.permissions || []).length
        ? `<div class="small text-secondary mt-1">Permissions: ${e.permissions.map(escapeHtml).join(', ')}</div>`
        : '';
      html += `<div class="col-12 col-md-6 col-xl-4">
        <div class="card bg-dark-card border-0 shadow-sm h-100">
          <div class="card-body p-4">
            <div class="d-flex align-items-start justify-content-between gap-2">
              <div>
                <div class="fw-bold">${escapeHtml(e.name || e.id)}</div>
                <div class="text-secondary small font-monospace">${escapeHtml(e.id)}</div>
              </div>
              ${pluginTrustBadge(e.trust_level || 'community')}
            </div>
            <p class="text-secondary small mt-2 mb-2">${escapeHtml(e.description || '')}</p>
            <div class="small text-secondary">${escapeHtml(e.source_repo_name || '')}</div>
            ${perms}
            <div class="d-flex align-items-center justify-content-between mt-3">
              ${pluginCompatBadge(e)}
              ${action}
            </div>
          </div>
        </div>
      </div>`;
    }
    html += '</div>';
    container.innerHTML = html;
  } catch (err) {
    container.innerHTML = `<div class="alert alert-danger">Error loading marketplace: ${escapeHtml(err.message)}</div>`;
  }
}

async function installPlugin(pluginId, version) {
  // Community trust confirmation dialog (PLUGIN_ARCHITECTURE.md §6).
  showConfirmModal(
    `Install ${pluginId}`,
    `<div class="small">
      <p>This will download and install <strong>${escapeHtml(pluginId)}</strong> v${escapeHtml(version)} from a third-party repository.</p>
      <p class="text-warning"><i class="fas fa-exclamation-triangle me-1"></i>Plugins run in-process. Only install from sources you trust.</p>
    </div>`,
    async () => {
      try {
        const resp = await fetch('/api/plugins/install', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ plugin_id: pluginId, version }),
        });
        const data = await resp.json();
        if (!resp.ok) { showToast(data.error || 'Install failed', 'danger'); return; }
        showToast(`Installed ${pluginId} v${data.version}`, 'success');
        hideConfirmModal();
        loadPluginsPage();
      } catch (e) {
        showToast('Network error: ' + e.message, 'danger');
      }
    },
    'Install anyway', 'btn-danger'
  );
}

async function loadRepositoriesPage() {
  const container = document.getElementById('repositoriesContainer');
  if (!container) return;
  try {
    const resp = await fetch('/api/plugins/repositories');
    const repos = await resp.json();
    let html = `<div class="card bg-dark-card border-0 shadow-sm mb-4">
      <div class="card-body p-4">
        <h5 class="fw-bold m-0"><i class="fas fa-plus-circle text-danger me-2"></i>Add Repository</h5>
        <p class="text-secondary small mb-3">Paste a plugin repository index URL (a JSON file with a "plugins" array).</p>
        <div class="d-flex gap-2">
          <input type="url" class="form-control" id="repoUrlInput" placeholder="https://example.com/plugins/index.json">
          <button class="btn btn-brand text-nowrap" onclick="addRepository()"><i class="fas fa-plus me-1"></i>Add</button>
        </div>
      </div>
    </div>`;

    if (!repos.length) {
      html += '<div class="card bg-dark-card border-0 shadow-sm"><div class="card-body p-4 text-secondary text-center">No repositories configured.</div></div>';
    } else {
      html += `<div class="card bg-dark-card border-0 shadow-sm">
        <div class="card-body p-4">
          <h5 class="fw-bold m-0 mb-3"><i class="fas fa-database text-danger me-2"></i>Configured Repositories</h5>
          <div class="table-responsive"><table class="table table-dark table-hover m-0 align-middle">
            <thead class="table-secondary text-secondary small text-uppercase" style="letter-spacing: 0.5px;">
              <tr><th>Name</th><th>URL</th><th>Last Synced</th><th class="text-end">Actions</th></tr>
            </thead><tbody>`;
      for (const r of repos) {
        html += `<tr>
          <td class="fw-bold">${escapeHtml(r.name)}</td>
          <td class="text-secondary small" style="word-break: break-all;">${escapeHtml(r.url)}</td>
          <td class="text-secondary small">${r.last_synced_at ? escapeHtml(r.last_synced_at.replace('T', ' ').slice(0, 19)) : '—'}</td>
          <td class="text-end">
            <button class="btn btn-sm btn-outline-info btn-icon" title="Refresh" onclick="refreshRepository(${r.id})"><i class="fas fa-sync"></i></button>
            <button class="btn btn-sm btn-outline-danger btn-icon" title="Remove" onclick="removeRepository(${r.id})"><i class="fas fa-trash"></i></button>
          </td>
        </tr>`;
      }
      html += '</tbody></table></div></div></div>';
    }
    container.innerHTML = html;
  } catch (e) {
    container.innerHTML = `<div class="alert alert-danger">Error loading repositories: ${escapeHtml(e.message)}</div>`;
  }
}

async function addRepository() {
  const url = document.getElementById('repoUrlInput').value.trim();
  if (!url) { showToast('Enter a repository URL', 'danger'); return; }
  try {
    const resp = await fetch('/api/plugins/repositories', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const data = await resp.json();
    if (!resp.ok) { showToast(data.error || 'Failed', 'danger'); return; }
    showToast(`Added repository '${data.name}'`, 'success');
    loadRepositoriesPage();
  } catch (e) {
    showToast('Network error: ' + e.message, 'danger');
  }
}

async function refreshRepository(repoId) {
  try {
    const resp = await fetch(`/api/plugins/repositories/${repoId}/refresh`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) { showToast(data.error || 'Failed', 'danger'); return; }
    showToast('Repository refreshed', 'success');
    loadRepositoriesPage();
  } catch (e) {
    showToast('Network error: ' + e.message, 'danger');
  }
}

async function removeRepository(repoId) {
  showConfirmModal('Remove Repository', 'Remove this repository? Installed plugins stay.', async () => {
    try {
      const resp = await fetch(`/api/plugins/repositories/${repoId}`, { method: 'DELETE' });
      if (!resp.ok) { showToast('Failed', 'danger'); return; }
      showToast('Repository removed', 'success');
      hideConfirmModal();
      loadRepositoriesPage();
    } catch (e) {
      showToast('Network error: ' + e.message, 'danger');
    }
  }, 'Remove', 'btn-danger');
}

// Uninstall action on the Installed tab (non-bundled plugins only).
async function uninstallPlugin(pluginId) {
  showConfirmModal(`Uninstall ${pluginId}`,
    'Remove this plugin? Its files and settings will be deleted.',
    async () => {
      try {
        const resp = await fetch(`/api/plugins/${encodeURIComponent(pluginId)}/uninstall`, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) { showToast(data.error || 'Uninstall failed', 'danger'); return; }
        showToast(`Uninstalled ${pluginId}`, 'success');
        hideConfirmModal();
        loadPluginsPage();
      } catch (e) {
        showToast('Network error: ' + e.message, 'danger');
      }
    }, 'Uninstall', 'btn-danger');
}

// Tab wiring: lazy-load Marketplace/Repositories on first click.
document.addEventListener('DOMContentLoaded', () => {
  const mkTab = document.getElementById('marketplace-tab');
  const repoTab = document.getElementById('repositories-tab');
  if (mkTab) mkTab.addEventListener('click', () => { if (!_marketplaceLoaded) { _marketplaceLoaded = true; loadMarketplacePage(); } });
  if (repoTab) repoTab.addEventListener('click', () => { if (!_repositoriesLoaded) { _repositoriesLoaded = true; loadRepositoriesPage(); } });
});
let _marketplaceLoaded = false;
let _repositoriesLoaded = false;
