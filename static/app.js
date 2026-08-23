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
  document.getElementById('addArtistModalTitle').textContent = `Add "${artistData.name}"`;
  document.getElementById('addArtistImg').src = artistData.image_url || '';
  document.getElementById('addArtistName').textContent = artistData.name;

  // Defaults
  document.getElementById('filterRemixes').checked = true;
  document.getElementById('filterLofi').checked = true;
  document.getElementById('filterLive').checked = true;
  document.getElementById('filterCompilations').checked = true;
  document.getElementById('incAlbums').checked = true;
  document.getElementById('incSingles').checked = true;
  document.getElementById('incCompilations').checked = false;
  document.getElementById('optMonitored').checked = true;
  document.getElementById('optAutoDownload').checked = false;

  document.getElementById('addArtistModal').classList.remove('d-none');
}

function hideAddArtistModal() {
  document.getElementById('addArtistModal').classList.add('d-none');
  _pendingAddArtistData = null;
}

async function confirmAddArtist() {
  if (!_pendingAddArtistData) return;
  const artistData = _pendingAddArtistData;
  const payload = {
    id: artistData.id,
    filter_remixes: document.getElementById('filterRemixes').checked,
    filter_lofi: document.getElementById('filterLofi').checked,
    filter_live: document.getElementById('filterLive').checked,
    filter_compilations: document.getElementById('filterCompilations').checked,
    include_albums: document.getElementById('incAlbums').checked,
    include_singles: document.getElementById('incSingles').checked,
    include_compilations: document.getElementById('incCompilations').checked,
    monitored: document.getElementById('optMonitored').checked,
    auto_download: document.getElementById('optAutoDownload').checked,
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
      loadDashboardArtists();
    } else {
      showToast(data.error || 'Failed to add artist', 'error');
    }
  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
  }
}

// ----- Dashboard Artists Loader -----
async function loadDashboardArtists() {
  const grid = document.getElementById('artistsDashboardGrid');
  if (!grid) return;

  try {
    const [respArtists, respStats] = await Promise.all([
      fetch('/api/artists'),
      fetch('/api/stats'),
    ]);
    const artists = await respArtists.json();
    renderDashboardArtists(artists);

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
let _selectedImportFolder = null;
let _selectArtistSearchDebounce = null;

async function loadImportCandidates() {
  const tbody = document.getElementById('importCandidatesTbody');
  const loading = document.getElementById('importLoading');
  if (!tbody) return;

  if (loading) loading.classList.remove('d-none');
  tbody.innerHTML = `<tr><td colspan="5" class="text-center text-secondary py-4"><div class="spinner-border spinner-border-sm text-danger me-2" role="status"></div>Scanning /music directory...</td></tr>`;
  try {
    const resp = await fetch('/api/import/candidates');
    const candidates = await resp.json();
    _importCandidates = candidates;
    renderImportCandidates(candidates);
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="5" class="text-danger py-3">Failed to scan: ${escapeHtml(err.message)}</td></tr>`;
  } finally {
    if (loading) loading.classList.add('d-none');
  }
}

function renderImportCandidates(candidates) {
  const tbody = document.getElementById('importCandidatesTbody');
  if (!tbody) return;

  if (!Array.isArray(candidates) || candidates.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" class="text-center text-secondary py-4">No audio folders found in /music</td></tr>';
    return;
  }

  let html = '';
  for (const c of candidates) {
    const isImported = c.is_already_imported;
    const suggested = c.suggested_deezer;

    html += `
      <tr>
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
              <button class="btn btn-outline-secondary btn-sm py-0 px-2" style="font-size:0.75rem;" title="Change Deezer artist match" onclick="openSelectArtistModal('${escapeHtml(c.folder_name)}')">
                <i class="fas fa-search me-1"></i>Change
              </button>` : ''
            }
          </div>
        </td>
        <td>${c.album_count} albums / ${c.track_count} tracks</td>
        <td>
          ${isImported
            ? '<span class="badge bg-success-subtle text-success"><i class="fas fa-check-circle me-1"></i>Managed</span>'
            : '<span class="badge bg-secondary-subtle text-secondary"><i class="fas fa-arrow-circle-right me-1"></i>Ready</span>'
          }
        </td>
        <td class="text-end">
          ${!isImported ? `
            <button class="btn btn-brand btn-sm" id="btn_import_${escapeHtml(c.folder_name).replace(/[^a-zA-Z0-9]/g, '_')}" onclick="importArtistFolder('${escapeHtml(c.folder_name)}', ${suggested ? suggested.id : 'null'}, this)">
              <i class="fas fa-file-import me-1"></i>Import
            </button>` : `<a href="/artist/${c.existing_artist_id}" class="btn btn-outline-secondary btn-sm"><i class="fas fa-external-link-alt me-1"></i>View</a>`
          }
        </td>
      </tr>`;
  }
  tbody.innerHTML = html;
}

function openSelectArtistModal(folderName) {
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
  const cand = _importCandidates.find(c => c.folder_name === folderName);
  if (cand && Array.isArray(cand.alternate_matches) && cand.alternate_matches.length > 0) {
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

async function importArtistFolder(folderName, deezerId, btnEl) {
  try {
    if (btnEl) {
      btnEl.disabled = true;
      btnEl.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i>Importing...';
    }
    showToast(`Starting import for folder '${folderName}'...`, 'info');

    const resp = await fetch('/api/import/folder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder_name: folderName, deezer_id: deezerId }),
    });
    const data = await resp.json();
    if (resp.ok) {
      showToast(`Successfully imported '${data.artist_name}': ${data.matched_tracks} tracks mapped!`, 'success');
      loadImportCandidates();
    } else {
      showToast(data.error || 'Import failed', 'error');
      if (btnEl) {
        btnEl.disabled = false;
        btnEl.innerHTML = '<i class="fas fa-file-import me-1"></i>Import';
      }
    }
  } catch (err) {
    showToast('Network error: ' + err.message, 'error');
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.innerHTML = '<i class="fas fa-file-import me-1"></i>Import';
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
function debouncedLoadDashboard(delay = 300) {
  clearTimeout(_dashboardReloadDebounce);
  _dashboardReloadDebounce = setTimeout(() => {
    if (document.getElementById('artistsDashboardGrid')) {
      loadDashboardArtists();
    }
  }, delay);
}

socket.on('artist_updated', (data) => {
  debouncedLoadDashboard(300);
  if (window._currentArtistId && data && data.artist_id === window._currentArtistId) {
    loadArtistDetailPage(window._currentArtistId);
  }
});

socket.on('artist_added', () => {
  debouncedLoadDashboard(150);
});

socket.on('artist_synced', (data) => {
  debouncedLoadDashboard(150);
  if (window._currentArtistId && data && data.artist_id === window._currentArtistId) {
    loadArtistDetailPage(window._currentArtistId);
  }
});

socket.on('artist_deleted', () => {
  debouncedLoadDashboard(100);
});

socket.on('toast', (data) => {
  showToast(data.message, data.type || 'info');
});

// ----- DOM Ready Initializer -----
document.addEventListener('DOMContentLoaded', () => {
  initArtistSearch();

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