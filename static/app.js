// ─── State ────────────────────────────────────────────────────────────────────
let jobId       = null;
let es          = null;
let _fileIdx    = 0;  // for staggered file-item entrance animation
let _jobRunning = false;  // true while any conversion is in progress

// Batch state (multi-EPUB)
let batchJobIds = [];
let batchSources = {};
let batchState   = {};

// ─── Docker detection ─────────────────────────────────────────────────────────
fetch('/config').then(r => r.json()).then(d => {
  if (!d.docker) return;
  // Hide folder picker button — can't open native dialog in a headless container
  const btn = document.getElementById('folder-btn');
  if (btn) btn.style.display = 'none';
  // Make path input editable so users can type a container-internal path
  const inp = document.getElementById('out_dir');
  if (inp) {
    inp.removeAttribute('readonly');
    inp.style.cursor = '';
    inp.placeholder = 'Default — files saved to audiobook_output/ on your host';
  }
  // Update hint text
  const hint = document.querySelector('#out_dir')?.closest('.field')?.querySelector('div[style*="margin-top"]');
  if (hint) hint.textContent = 'Running in Docker. Output files appear in the audiobook_output/ folder next to your docker-compose.yml.';
  // In Docker, localhost = the container. Ollama runs on the host, so use host.docker.internal.
  const ollamaInp = document.getElementById('ollama_url');
  if (ollamaInp && ollamaInp.value.includes('localhost')) {
    ollamaInp.value = 'http://host.docker.internal:11434';
  }
}).catch(() => {});

// ─── File drag & drop ─────────────────────────────────────────────────────────
const dz  = document.getElementById('drop-zone');
const inp = document.getElementById('epub-file');

inp.addEventListener('change', e => {
  if (e.target.files.length) showFiles(e.target.files);
});

dz.addEventListener('dragover',  e => { e.preventDefault(); dz.classList.add('drag-over'); });
dz.addEventListener('dragleave', () => dz.classList.remove('drag-over'));
dz.addEventListener('drop', e => {
  e.preventDefault(); dz.classList.remove('drag-over');
  const all   = Array.from(e.dataTransfer.files);
  const epubs = all.filter(f => f.name.toLowerCase().endsWith('.epub'));
  if (epubs.length) {
    inp.files = e.dataTransfer.files;
    showFiles(e.dataTransfer.files);
    dz.classList.add('drop-flash');
    dz.addEventListener('animationend', () => dz.classList.remove('drop-flash'), {once:true});
  } else {
    toast('Please drop .epub files only');
  }
});

function showFiles(fileList) {
  dz.classList.add('has-file');
  const n = document.getElementById('up-name');
  if (fileList.length === 1) {
    const f = fileList[0];
    n.textContent = f.name + '  (' + fmtBytes(f.size) + ')';
    dz.querySelector('.up-title').textContent = 'EPUB loaded';
    dz.querySelector('.up-sub').textContent   = 'Click to replace';
    fetchChapters(f);
  } else {
    const total = Array.from(fileList).reduce((s, f) => s + f.size, 0);
    n.textContent = fileList.length + ' books selected  (' + fmtBytes(total) + ')';
    dz.querySelector('.up-title').textContent = fileList.length + ' EPUBs loaded';
    dz.querySelector('.up-sub').textContent   = 'Click to change selection';
    // Hide chapter card for multi-book batch
    document.getElementById('chapter-card').style.display = 'none';
    _chaptersData = [];
  }
  n.style.display = 'block';
}

function fmtBytes(b) {
  if (b < 1024)    return b + ' B';
  if (b < 1048576) return (b/1024).toFixed(1) + ' KB';
  return (b/1048576).toFixed(1) + ' MB';
}

// ─── Chapter selection ────────────────────────────────────────────────────────
let _chaptersData = [];

async function fetchChapters(file) {
  const card  = document.getElementById('chapter-card');
  const list  = document.getElementById('ch-list');
  const tools = document.getElementById('ch-card-tools');

  card.style.display  = 'block';
  tools.style.display = 'none';
  list.innerHTML = '<div class="ch-loading"><span class="spinner-sm"></span> Reading chapters…</div>';
  _chaptersData = [];

  const minChLenEl = document.getElementById('min_ch_len');
  const minChLen   = minChLenEl ? minChLenEl.value : '200';

  const fd = new FormData();
  fd.append('file',       file);
  fd.append('min_ch_len', minChLen);

  try {
    const r = await fetch('/chapters', { method: 'POST', body: fd });
    if (!r.ok) {
      let detail = '';
      try { detail = (await r.json()).detail || ''; } catch(_) {}
      throw new Error('HTTP ' + r.status + (detail ? ': ' + detail : ''));
    }
    const d = await r.json();
    _chaptersData = d.chapters;
    _renderChapters(d.chapters);
    tools.style.display = 'flex';
  } catch(e) {
    console.error('fetchChapters error:', e);
    list.innerHTML = '<div class="ch-msg">⚠ Could not read chapters (' + e.message + '). All chapters will be converted.</div>';
  }
}

function _renderChapters(chapters) {
  const list = document.getElementById('ch-list');
  if (!chapters || !chapters.length) {
    list.innerHTML = '<div class="ch-msg">No chapters found above minimum length.</div>';
    return;
  }
  list.innerHTML = chapters.map(ch =>
    '<label class="ch-row chk">' +
      '<input type="checkbox" class="ch-chk" data-index="' + ch.index + '" checked>' +
      '<span class="ch-title">' + esc(ch.title) + '</span>' +
      '<span class="ch-chars">' + _fmtChars(ch.chars) + '</span>' +
    '</label>'
  ).join('');
  list.querySelectorAll('.ch-chk').forEach(cb =>
    cb.addEventListener('change', _updateChapterCount)
  );
  _updateChapterCount();
}

function _updateChapterCount() {
  const total   = document.querySelectorAll('.ch-chk').length;
  const checked = document.querySelectorAll('.ch-chk:checked').length;
  const lbl     = document.getElementById('ch-count-lbl');
  if (lbl) lbl.textContent = checked + ' / ' + total + ' selected';
}

function selectAllChapters(val) {
  document.querySelectorAll('.ch-chk').forEach(cb => { cb.checked = val; });
  _updateChapterCount();
}

function _getChapterIndices() {
  const all     = [...document.querySelectorAll('.ch-chk')];
  const checked = all.filter(cb => cb.checked);
  if (!all.length || checked.length === all.length) return ''; // empty = all
  return checked.map(cb => cb.dataset.index).join(',');
}

function _fmtChars(n) {
  return n >= 1000 ? (n / 1000).toFixed(1) + 'k chars' : n + ' chars';
}

// ─── Speed ────────────────────────────────────────────────────────────────────
const SPEED_PRESETS = [0.75, 1.0, 1.25, 1.5, 1.75, 2.0];
function _updateSpd(v) {
  const val = +v;
  // update display
  const el = document.getElementById('speed-val');
  el.textContent = val.toFixed(2) + ' ×';
  el.classList.remove('bump');
  void el.offsetWidth;
  el.classList.add('bump');
  // highlight nearest dot
  document.querySelectorAll('.stk').forEach((btn, i) => {
    btn.classList.toggle('active', Math.abs(SPEED_PRESETS[i] - val) < 0.01);
  });
}
document.getElementById('speed').addEventListener('input', e => _updateSpd(e.target.value));
function setSpd(v) {
  document.getElementById('speed').value = v;
  _updateSpd(v);
}
// init active dot on load
_updateSpd(0.95);

// ─── Panel switching ──────────────────────────────────────────────────────────
function switchToOutput() {
  const left   = document.getElementById('left');
  const out    = document.querySelector('.out-sticky');
  const main   = document.querySelector('main');
  const banner = document.getElementById('job-banner');

  if (banner) banner.style.display = 'none';

  // Desktop two-column layout: both panels always visible — just activate output
  if (window.innerWidth >= 960) {
    main.classList.add('has-job');
    out.classList.add('active');
    return;
  }

  // Mobile: animate left panel out, show output
  left.classList.add('slide-out');
  setTimeout(() => {
    left.style.display = 'none';
    left.classList.remove('slide-out');
    main.classList.add('has-job');
    out.classList.add('active');
  }, 250);
}

function backToSettings() {
  const left   = document.getElementById('left');
  const out    = document.querySelector('.out-sticky');
  const main   = document.querySelector('main');
  const banner = document.getElementById('job-banner');

  if (_jobRunning) {
    if (banner) banner.style.display = 'flex';
  } else {
    Object.values(batchSources).forEach(s => s.close());
    batchSources = {};
    if (banner) banner.style.display = 'none';
  }

  // Desktop: both panels always visible, nothing to swap
  if (window.innerWidth >= 960) return;

  // Mobile: restore settings panel
  out.classList.remove('active');
  main.classList.remove('has-job');
  left.style.display = '';
  void left.offsetWidth;
  left.style.animation = 'fadeUp .35s ease both';
  setTimeout(() => { left.style.animation = ''; }, 400);
}

// ─── Folder picker ────────────────────────────────────────────────────────────
async function pickFolder() {
  const btn = document.getElementById('folder-btn');
  btn.disabled = true; btn.textContent = '…';
  try {
    const r = await fetch('/pick-folder');
    const d = await r.json();
    if (d.path) {
      document.getElementById('out_dir').value = d.path;
    } else {
      toast('No folder selected');
    }
  } catch(e) {
    toast('Folder picker unavailable');
  }
  btn.disabled = false; btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
}

// ─── Advanced toggle ──────────────────────────────────────────────────────────
function toggleAdv() {
  document.getElementById('adv-btn').classList.toggle('open');
  document.getElementById('adv-body').classList.toggle('open');
}

// ─── Multi-voice toggle ───────────────────────────────────────────────────────
function toggleMultiVoice() {
  const enabled = document.getElementById('multi_voice').checked;
  document.getElementById('mv-settings').style.display = enabled ? 'block' : 'none';
}

// ─── Format toggle ────────────────────────────────────────────────────────────
function onFormatChange() {
  const fmt = document.querySelector('input[name="output_format"]:checked').value;
  document.getElementById('bitrate-row').classList.toggle('visible', fmt === 'mp3');
}

// ─── Start ────────────────────────────────────────────────────────────────────
async function startJob() {
  if (!inp.files || inp.files.length === 0) {
    toast('Please select at least one EPUB file'); return;
  }

  const fd = new FormData();
  for (const f of inp.files) { fd.append('files', f); }
  fd.append('voice',         document.getElementById('voice').value);
  fd.append('lang_code',     document.getElementById('lang').value);
  fd.append('speed',         document.getElementById('speed').value);
  fd.append('device',        document.getElementById('device').value);
  fd.append('trf',           document.getElementById('trf').checked);
  fd.append('merge',         document.getElementById('merge').checked);
  fd.append('chunk_size',    document.getElementById('chunk_size').value);
  fd.append('silence',       document.getElementById('silence').value);
  fd.append('min_ch_len',    document.getElementById('min_ch_len').value);
  fd.append('num_workers',   document.getElementById('num_workers').value);
  fd.append('output_format',   document.querySelector('input[name="output_format"]:checked').value);
  fd.append('bitrate',         document.getElementById('bitrate').value);
  fd.append('custom_out_dir',  document.getElementById('out_dir').value.trim());
  fd.append('chapter_indices', _getChapterIndices());
  fd.append('enhance',         document.getElementById('enhance').checked);
  fd.append('multi_voice',     document.getElementById('multi_voice').checked);
  fd.append('ollama_url',      document.getElementById('ollama_url').value.trim());
  fd.append('ollama_model',    document.getElementById('ollama_model').value);

  switchToOutput();
  resetOutput();

  // Show output path immediately
  const outDir   = document.getElementById('out_dir').value.trim();
  const pathCard = document.getElementById('out-path-card');
  const pathVal  = document.getElementById('out-path-val');
  if (pathCard) pathCard.style.display = 'block';
  if (pathVal)  pathVal.textContent    = outDir || 'audiobook_output/ (default)';

  _jobRunning = true;
  setDot('running'); setStatus('Starting…');
  document.getElementById('start-btn').disabled = true;
  document.getElementById('start-btn').textContent = 'Converting…';
  document.getElementById('stop-btn').style.display = 'inline-flex';

  try {
    const r = await fetch('/convert', { method: 'POST', body: fd });
    if (!r.ok) throw new Error('Server error ' + r.status);
    const d = await r.json();

    batchJobIds = d.job_ids;

    if (batchJobIds.length === 1) {
      // Single-book path — use existing log+files UI unchanged
      jobId = batchJobIds[0];
      setStatus('Starting…');
      connectSSE(jobId);
    } else {
      // Multi-book batch path
      initBatchUI(d);
      connectBatchSSE(batchJobIds);
    }
  } catch (e) {
    setDot('error'); setStatus('Error');
    addLog('Error: ' + e.message, 'err');
    resetBtns();
  }
}

// ─── SSE (single-book) ────────────────────────────────────────────────────────
function connectSSE(id) {
  if (es) es.close();
  es = new EventSource('/stream/' + id);
  es.onmessage = e => { try { handleMsg(JSON.parse(e.data)); } catch(_) {} };
  es.onerror   = () => { es.close(); addLog('Connection lost.', 'warn'); };
}

function handleMsg(m) {
  if (m.type === 'log') {
    if (m.msg.includes('[Ollama]')) addOllamaLog(m.msg);
    else addLog(m.msg);
  }
  else if (m.type === 'status')   setStatus(m.msg);
  else if (m.type === 'progress') setProg(m.value, m.label);
  else if (m.type === 'file')     { addFile(m); if (m.chapter > 0) _chDone(m.chapter - 1, m.duration); }
  else if (m.type === 'done')     onDone(m.files);
  else if (m.type === 'ch_info')  _initChGrid(m.chapters);
  else if (m.type === 'ch_start') _chStart(m.ch_i, m.chunks);
  else if (m.type === 'ch_prog')  _chProg(m.ch_i, m.pct);
  else if (m.type === 'ch_skip')  _chSkip(m.ch_i);
}

function onDone(files) {
  if (es) es.close();
  _jobRunning = false;
  setDot('done'); setStatus('Done!');
  setProg(1, 'Conversion complete');
  resetBtns();
  const banner = document.getElementById('job-banner');
  if (banner) banner.style.display = 'none';
  if (files && files.length) toast('✓ ' + files.length + ' file(s) ready');
}

// ─── SSE (batch, multi-book) ──────────────────────────────────────────────────
function connectBatchSSE(jobIds) {
  batchState   = {};
  batchSources = {};

  jobIds.forEach((id, i) => {
    batchState[id] = {
      status: i === 0 ? 'running' : 'queued',
      title:  batchJobIds[i] ? (window._batchTitles && window._batchTitles[i]) || ('Book ' + (i+1)) : ('Book ' + (i+1)),
      lastLog: i === 0 ? 'Starting…' : 'Waiting in queue…',
      files:   [],
      totalDur: 0,
    };
    const src = new EventSource('/stream/' + id);
    src.onmessage = e => { try { handleBatchMsg(id, JSON.parse(e.data)); } catch(_) {} };
    src.onerror   = () => {
      src.close();
      batchState[id].status = 'error';
      updateBookCard(id);
      checkBatchComplete();
    };
    batchSources[id] = src;
  });
}

function handleBatchMsg(id, m) {
  const s = batchState[id];
  if (!s) return;

  if (m.type === 'log') {
    const txt = m.msg.trim();
    if (!txt.startsWith('[MEM]')) {
      if (s.status === 'queued') s.status = 'running';
      s.lastLog = txt; updateBookCard(id);
    }
  } else if (m.type === 'status') {
    if (s.status === 'queued') s.status = 'running';
    s.lastLog = m.msg;
    updateBookCard(id);
  } else if (m.type === 'progress') {
    s.progress = m.value;
    updateBookCard(id);
  } else if (m.type === 'file') {
    s.files.push(m);
    s.totalDur += (m.duration || 0);
    updateBookCard(id);
  } else if (m.type === 'done') {
    if (batchSources[id]) { batchSources[id].close(); delete batchSources[id]; }
    s.status    = 'done';
    s.doneFiles = m.files || [];
    updateBookCard(id);
    checkBatchComplete();
  }
}

function checkBatchComplete() {
  const allDone = batchJobIds.every(id =>
    batchState[id] && (batchState[id].status === 'done' || batchState[id].status === 'error') &&
    batchState[id].status !== 'queued'
  );
  if (allDone) {
    const doneCount  = batchJobIds.filter(id => batchState[id] && batchState[id].status === 'done').length;
    const errorCount = batchJobIds.filter(id => batchState[id] && batchState[id].status === 'error').length;
    _jobRunning = false;
    setDot('done');
    setStatus('Complete — ' + doneCount + ' book(s) converted' +
              (errorCount ? ', ' + errorCount + ' error(s)' : ''));
    setProg(1, '');
    resetBtns();
    const banner = document.getElementById('job-banner');
    if (banner) banner.style.display = 'none';
    toast('✓ ' + doneCount + ' book(s) converted successfully');
  } else {
    const pending = batchJobIds.filter(id => batchState[id] && batchState[id].status === 'running').length;
    setStatus('Converting ' + pending + ' of ' + batchJobIds.length + ' books…');
  }
}

// ─── Batch UI ─────────────────────────────────────────────────────────────────
function initBatchUI(d) {
  // Seed titles immediately from server response
  window._batchTitles = d.titles || [];
  d.job_ids.forEach((id, i) => {
    if (batchState[id]) batchState[id].title = d.titles[i] || ('Book ' + (i+1));
  });

  // Replace the out-grid with a batch-grid of book cards
  const outGrid = document.getElementById('out-grid');
  outGrid.className = 'batch-grid';
  outGrid.innerHTML = '';

  d.job_ids.forEach((id, i) => {
    const title = (d.titles && d.titles[i]) ? d.titles[i] : ('Book ' + (i+1));
    // Seed state immediately so updateBookCard works
    if (!batchState[id]) {
      batchState[id] = { status:'running', title, lastLog:'Starting…', files:[], totalDur:0 };
    } else {
      batchState[id].title = title;
    }
    const card = document.createElement('div');
    card.className = 'book-card';
    card.id = 'book-card-' + id;
    card.innerHTML = bookCardHTML(id, title);
    outGrid.appendChild(card);
  });

  setStatus('Converting ' + d.job_ids.length + ' books…');
}

function bookCardHTML(id, title) {
  return (
    '<div class="bc-header">' +
      '<span class="bc-title" id="bc-title-' + id + '">' + esc(title) + '</span>' +
      '<span class="bc-badge running" id="bc-badge-' + id + '"><span class="spinner"></span></span>' +
    '</div>' +
    '<div class="bc-bar-wrap" id="bc-prog-' + id + '" style="display:none">' +
      '<div class="bc-bar-fill" id="bc-prog-fill-' + id + '"></div>' +
    '</div>' +
    '<div class="bc-info" id="bc-log-' + id + '">Starting…</div>'
  );
}

function updateBookCard(id) {
  const s    = batchState[id];
  const card = document.getElementById('book-card-' + id);
  if (!card || !s) return;

  // Title
  const titleEl = document.getElementById('bc-title-' + id);
  if (titleEl && s.title) titleEl.textContent = s.title;

  // Badge
  const badgeEl = document.getElementById('bc-badge-' + id);
  if (badgeEl) {
    if (s.status === 'done') {
      badgeEl.textContent = '✓ Done';
      badgeEl.className = 'bc-badge done';
      card.classList.add('bc-done');
    } else if (s.status === 'error') {
      badgeEl.textContent = '✗ Error';
      badgeEl.className = 'bc-badge error';
      card.classList.add('bc-error');
    } else if (s.status === 'queued') {
      badgeEl.textContent = '· Queued';
      badgeEl.className = 'bc-badge queued';
    } else {
      badgeEl.innerHTML = '<span class="spinner"></span>';
      badgeEl.className = 'bc-badge running';
    }
  }

  // Info line
  const infoEl = document.getElementById('bc-log-' + id);
  if (infoEl) {
    if (s.status === 'done') {
      const mins = Math.floor(s.totalDur / 60);
      const secs = Math.round(s.totalDur % 60);
      const dur  = mins > 0 ? mins + 'h ' + secs + 'm' : secs + 's';
      infoEl.textContent = (s.doneFiles ? s.doneFiles.length : s.files.length) + ' files  ·  ' + dur;
    } else {
      const txt = s.lastLog || 'Processing…';
      infoEl.textContent = txt.length > 72 ? txt.slice(0, 69) + '…' : txt;
    }
  }

  // Progress bar
  if (typeof s.progress === 'number') {
    const progEl = document.getElementById('bc-prog-' + id);
    if (progEl) {
      progEl.style.display = 'block';
      const fill = document.getElementById('bc-prog-fill-' + id);
      if (fill) fill.style.width = Math.round(s.progress * 100) + '%';
    }
  }
}

function downloadAll(jobId, filenames) {
  filenames.forEach((fname, i) => {
    setTimeout(() => {
      const a = document.createElement('a');
      a.href     = '/download/' + jobId + '/' + encodeURIComponent(fname);
      a.download = fname;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    }, i * 300);
  });
}

// ─── Chapter progress grid ────────────────────────────────────────────────────
let _chState = {};   // ch_i → {status, pct, totalChunks}
let _chTotal = 0;

function _initChGrid(chapters) {
  _chState  = {};
  _chTotal  = chapters.length;
  const wrap = document.getElementById('ch-prog-wrap');
  const grid = document.getElementById('ch-prog-grid');
  if (!wrap || !grid) return;
  wrap.style.display = 'block';
  grid.innerHTML = chapters.map(ch => {
    _chState[ch.i] = { status: 'pending', pct: 0 };
    return (
      '<div class="ch-cell" id="ch-cell-' + ch.i + '" data-status="pending">' +
        '<div class="ch-cell-top">' +
          '<span class="ch-cell-num">' + (ch.i + 1) + '</span>' +
          '<span class="ch-cell-title">' + esc(ch.title) + '</span>' +
          '<span class="ch-cell-badge" id="ch-badge-' + ch.i + '">Pending</span>' +
        '</div>' +
        '<div class="ch-cell-bar-wrap">' +
          '<div class="ch-cell-bar-fill" id="ch-bar-' + ch.i + '" style="width:0%"></div>' +
        '</div>' +
      '</div>'
    );
  }).join('');
  _updateChSummary();
}

function _chCellUpdate(i, status, pct, label) {
  const cell  = document.getElementById('ch-cell-'  + i);
  const badge = document.getElementById('ch-badge-' + i);
  const bar   = document.getElementById('ch-bar-'   + i);
  if (!cell) return;
  cell.dataset.status = status;
  if (badge) badge.textContent = label;
  if (bar)   bar.style.width   = Math.round(pct * 100) + '%';
  if (_chState[i]) { _chState[i].status = status; _chState[i].pct = pct; }
}

function _chStart(i, totalChunks) {
  if (_chState[i]) _chState[i].totalChunks = totalChunks;
  _chCellUpdate(i, 'running', 0, '0%');
  _updateChSummary();
}

function _chProg(i, pct) {
  _chCellUpdate(i, 'running', pct, Math.round(pct * 100) + '%');
}

function _chDone(i, dur) {
  const mins = Math.floor(dur / 60);
  const secs = Math.round(dur % 60);
  const lbl  = mins > 0 ? mins + 'm ' + secs + 's' : secs + 's';
  _chCellUpdate(i, 'done', 1, lbl);
  _updateChSummary();
}

function _chSkip(i) {
  _chCellUpdate(i, 'skipped', 1, 'Skipped');
  _updateChSummary();
}

function _updateChSummary() {
  const el = document.getElementById('ch-prog-summary');
  if (!el || !_chTotal) return;
  const done    = Object.values(_chState).filter(s => s.status === 'done').length;
  const running = Object.values(_chState).filter(s => s.status === 'running').length;
  el.textContent = done + ' / ' + _chTotal + ' done' + (running ? '  ·  ' + running + ' running' : '');
}

// ─── Stop ─────────────────────────────────────────────────────────────────────
function stopJob() {
  if (batchJobIds.length > 1) {
    batchJobIds.forEach(id => fetch('/stop/' + id, { method: 'POST' }));
    Object.values(batchSources).forEach(s => s.close());
    batchSources = {};
  } else if (jobId) {
    fetch('/stop/' + jobId, { method: 'POST' });
  }
  setStatus('Stopping…');
  document.getElementById('stop-btn').disabled = true;
}

// ─── UI helpers ───────────────────────────────────────────────────────────────
function setDot(state)  { document.getElementById('dot').className = 'dot ' + state; }
function setStatus(txt) { document.getElementById('status-txt').textContent = txt; }
function setProg(v, lbl) {
  document.getElementById('prog-fill').style.width = Math.round(v * 100) + '%';
  document.getElementById('prog-lbl').textContent  = lbl || '';
}

function addOllamaLog(raw) {
  const card = document.getElementById('ollama-log-card');
  const box  = document.getElementById('ollama-log');
  const sum  = document.getElementById('ollama-log-summary');
  if (!box) return;
  if (card) card.style.display = 'block';

  raw.split('\n').forEach(line => {
    line = line.trim();
    if (!line) return;
    // Strip the [Ollama] prefix for cleaner display
    const text = line.replace(/\[Ollama\]\s*/g, '').trim();
    if (!text) return;

    const d = document.createElement('div');
    d.className = 'ol-line';
    if (line.includes('!'))          d.classList.add('ol-err');
    else if (line.includes('←'))     d.classList.add('ol-recv');
    else if (line.includes('→'))     d.classList.add('ol-send');
    else if (line.includes('Characters')) d.classList.add('ol-map');
    d.textContent = text;
    box.appendChild(d);
  });

  // Update summary: count unique characters from "Characters so far:" lines
  const mapLine = [...box.querySelectorAll('.ol-map')].pop();
  if (mapLine && sum) sum.textContent = mapLine.textContent;

  box.scrollTop = box.scrollHeight;
}

function addLog(raw, cls) {
  const box = document.getElementById('log');
  if (!box) return;
  raw.split('\n').forEach(line => {
    if (!line.trim()) return;
    const d = document.createElement('div');
    d.className = 'll';
    if (cls)                                               d.classList.add(cls);
    else if (/✓|Done|Saved|ready/.test(line))             d.classList.add('ok');
    else if (/Error|error/.test(line))                    d.classList.add('err');
    else if (/!|Warning|warn/i.test(line))                d.classList.add('warn');
    else if (/^──|Initializing|Model ready/.test(line))   d.classList.add('hd');
    d.textContent = line;
    box.appendChild(d);
  });
  box.scrollTop = box.scrollHeight;
}

function addFile(f) {
  const list  = document.getElementById('file-list');
  if (!list) return;
  const empty = list.querySelector('.empty');
  if (empty) empty.remove();

  const mins = Math.floor(f.duration / 60);
  const secs = Math.round(f.duration % 60);
  const dur  = mins > 0 ? mins + 'm ' + secs + 's' : secs + 's';
  const icon = '';

  const chLabel = f.chapter === 0 ? 'Full Audiobook' : 'Chapter ' + f.chapter;

  const el = document.createElement('div');
  el.className = 'file-item' + (f.chapter === 0 ? ' full' : '');
  el.style.animationDelay = (_fileIdx++ * 0.07) + 's';
  el.innerHTML =
    '<div>' +
      '<div class="fi-ch-label">' + chLabel + ' · ' + dur + '</div>' +
      '<div class="fi-title">' + esc(f.title) + '</div>' +
    '</div>' +
    '<a class="btn-sm" href="/download/' + jobId + '/' +
      encodeURIComponent(f.filename) + '" download="' + esc(f.filename) + '">↓ Save</a>';
  list.appendChild(el);
}

function resetOutput() {
  // Close any open batch connections
  Object.values(batchSources).forEach(s => s.close());
  batchSources = {};
  batchState   = {};
  batchJobIds  = [];
  _fileIdx     = 0;
  window._batchTitles = [];

  // Reset chapter progress grid
  _chState = {}; _chTotal = 0;
  const wrap = document.getElementById('ch-prog-wrap');
  const grid = document.getElementById('ch-prog-grid');
  if (wrap) wrap.style.display = 'none';
  if (grid) grid.innerHTML = '';

  _jobRunning = false;
  // Hide job banner
  const banner = document.getElementById('job-banner');
  if (banner) banner.style.display = 'none';

  // Clear Ollama log
  const olCard = document.getElementById('ollama-log-card');
  const olBox  = document.getElementById('ollama-log');
  const olSum  = document.getElementById('ollama-log-summary');
  if (olCard) olCard.style.display = 'none';
  if (olBox)  olBox.innerHTML = '';
  if (olSum)  olSum.textContent = '';

  // Clear batch grid, hide out-path card, wipe log + file-list content
  const outGrid  = document.getElementById('out-grid');
  const pathCard = document.getElementById('out-path-card');
  const logEl    = document.getElementById('log');
  const flEl     = document.getElementById('file-list');
  if (outGrid)  { outGrid.className = 'out-grid'; outGrid.innerHTML = ''; }
  if (pathCard) pathCard.style.display = 'none';
  if (logEl)    logEl.innerHTML  = '';
  if (flEl)     flEl.innerHTML   = '';
  setProg(0, '');
}

function resetBtns() {
  const b = document.getElementById('start-btn');
  b.disabled = false; b.textContent = 'Start Converting';
  const s = document.getElementById('stop-btn');
  s.style.display = 'none'; s.disabled = false;
}

function clearLog() {
  const box = document.getElementById('log');
  if (box) box.innerHTML = '';
}

function esc(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ─── Voice preview ────────────────────────────────────────────────────────────
let _previewURL = null;

async function previewVoice() {
  const voice  = document.getElementById('voice').value;
  const btn    = document.getElementById('preview-btn');
  const status = document.getElementById('preview-status');
  const audio  = document.getElementById('preview-audio');

  if (btn.classList.contains('playing')) { stopPreview(); return; }

  btn.disabled = true; btn.textContent = '…';
  status.textContent = 'Loading ' + voice + '…';

  try {
    const resp = await fetch('/preview/' + voice);
    if (!resp.ok) throw new Error('server error ' + resp.status);
    const blob = await resp.blob();
    if (_previewURL) URL.revokeObjectURL(_previewURL);
    _previewURL = URL.createObjectURL(blob);
    audio.src = _previewURL;
    audio.onended = stopPreview;
    audio.onerror = () => { status.textContent = 'Playback error'; stopPreview(); };
    await audio.play();
    btn.disabled = false; btn.textContent = '■'; btn.classList.add('playing');
    status.textContent = 'Playing: ' + voice;
  } catch(e) {
    status.textContent = 'Preview unavailable — will work after first conversion run';
    btn.disabled = false; btn.textContent = '▶';
  }
}

function stopPreview() {
  const btn    = document.getElementById('preview-btn');
  const status = document.getElementById('preview-status');
  const audio  = document.getElementById('preview-audio');
  audio.pause(); audio.src = '';
  btn.classList.remove('playing'); btn.textContent = '▶'; btn.disabled = false;
  status.textContent = '';
}

async function stopApp() {
  const btn = document.getElementById('stop-app-btn');
  if (!confirm('Stop the ScrollTone server?')) return;
  btn.disabled = true;
  btn.innerHTML = '… Stopping';
  try {
    await fetch('/shutdown', { method: 'POST' });
  } catch(_) {}
  btn.innerHTML = '✓ Stopped';
  toast('Server stopped — you can close this tab.');
}

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3200);
}
