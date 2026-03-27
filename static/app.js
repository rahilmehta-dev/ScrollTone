// ─── State ────────────────────────────────────────────────────────────────────
let jobId    = null;
let es       = null;
let _fileIdx = 0;  // for staggered file-item entrance animation

// Batch state (multi-EPUB)
let batchJobIds = [];
let batchSources = {};
let batchState   = {};

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
  } else {
    const total = Array.from(fileList).reduce((s, f) => s + f.size, 0);
    n.textContent = fileList.length + ' books selected  (' + fmtBytes(total) + ')';
    dz.querySelector('.up-title').textContent = fileList.length + ' EPUBs loaded';
    dz.querySelector('.up-sub').textContent   = 'Click to change selection';
  }
  n.style.display = 'block';
}

function fmtBytes(b) {
  if (b < 1024)    return b + ' B';
  if (b < 1048576) return (b/1024).toFixed(1) + ' KB';
  return (b/1048576).toFixed(1) + ' MB';
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
_updateSpd(1.0);

// ─── Panel switching ──────────────────────────────────────────────────────────
function switchToOutput() {
  const left = document.getElementById('left');
  const out  = document.querySelector('.out-sticky');
  const main = document.querySelector('main');

  // Animate left panel out
  left.classList.add('slide-out');
  setTimeout(() => {
    left.style.display = 'none';
    left.classList.remove('slide-out');
    // Expand main and show output
    main.classList.add('has-job');
    out.classList.add('active');
  }, 250);
}

function backToSettings() {
  const left = document.getElementById('left');
  const out  = document.querySelector('.out-sticky');
  const main = document.querySelector('main');

  // Close all batch SSE connections
  Object.values(batchSources).forEach(s => s.close());
  batchSources = {};

  // Hide output, shrink main, restore settings
  out.classList.remove('active');
  main.classList.remove('has-job');
  left.style.display = '';
  // Re-trigger entrance animation
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
  fd.append('output_format',  document.querySelector('input[name="output_format"]:checked').value);
  fd.append('bitrate',        document.getElementById('bitrate').value);
  fd.append('custom_out_dir', document.getElementById('out_dir').value.trim());

  switchToOutput();
  resetOutput();
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
  if      (m.type === 'log')      addLog(m.msg);
  else if (m.type === 'status')   setStatus(m.msg);
  else if (m.type === 'progress') setProg(m.value, m.label);
  else if (m.type === 'file')     addFile(m);
  else if (m.type === 'done')     onDone(m.files);
}

function onDone(files) {
  if (es) es.close();
  setDot('done'); setStatus('Done!');
  setProg(1, 'Conversion complete');
  resetBtns();
  if (files && files.length) toast('✓ ' + files.length + ' file(s) ready');
}

// ─── SSE (batch, multi-book) ──────────────────────────────────────────────────
function connectBatchSSE(jobIds) {
  batchState   = {};
  batchSources = {};

  jobIds.forEach((id, i) => {
    batchState[id] = {
      status: 'running',
      title:  batchJobIds[i] ? (window._batchTitles && window._batchTitles[i]) || ('Book ' + (i+1)) : ('Book ' + (i+1)),
      lastLog: 'Starting…',
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
    s.lastLog = m.msg.trim();
    updateBookCard(id);
  } else if (m.type === 'status') {
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
    batchState[id] && (batchState[id].status === 'done' || batchState[id].status === 'error')
  );
  if (allDone) {
    const doneCount  = batchJobIds.filter(id => batchState[id] && batchState[id].status === 'done').length;
    const errorCount = batchJobIds.filter(id => batchState[id] && batchState[id].status === 'error').length;
    setDot('done');
    setStatus('Complete — ' + doneCount + ' book(s) converted' +
              (errorCount ? ', ' + errorCount + ' error(s)' : ''));
    setProg(1, '');
    resetBtns();
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
  const outGrid = document.querySelector('.out-grid');
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
      '<span class="bc-status-icon" id="bc-icon-' + id + '"><span class="spinner"></span></span>' +
    '</div>' +
    '<div class="bc-log" id="bc-log-' + id + '">Starting…</div>' +
    '<div class="bc-progress" id="bc-prog-' + id + '" style="display:none">' +
      '<div class="bc-prog-fill" id="bc-prog-fill-' + id + '"></div>' +
    '</div>' +
    '<div class="bc-actions" id="bc-actions-' + id + '" style="display:none"></div>'
  );
}

function updateBookCard(id) {
  const s    = batchState[id];
  const card = document.getElementById('book-card-' + id);
  if (!card || !s) return;

  // Title
  const titleEl = document.getElementById('bc-title-' + id);
  if (titleEl && s.title) titleEl.textContent = s.title;

  // Status icon
  const iconEl = document.getElementById('bc-icon-' + id);
  if (iconEl) {
    if (s.status === 'done') {
      iconEl.innerHTML = '<span class="bc-checkmark">✓</span>';
      card.classList.add('bc-done');
    } else if (s.status === 'error') {
      iconEl.innerHTML = '<span class="bc-errmark">✗</span>';
      card.classList.add('bc-error');
    } else {
      iconEl.innerHTML = '<span class="spinner"></span>';
    }
  }

  // Log / status line
  const logEl = document.getElementById('bc-log-' + id);
  if (logEl) {
    if (s.status === 'done') {
      const totalMins = Math.floor(s.totalDur / 60);
      const totalSecs = Math.round(s.totalDur % 60);
      const durTxt = totalMins > 0
        ? totalMins + 'h ' + totalSecs + 'm'
        : totalSecs + 's';
      logEl.textContent = (s.doneFiles ? s.doneFiles.length : s.files.length) + ' file(s)  ·  ' + durTxt;
    } else {
      const txt = s.lastLog || 'Processing…';
      logEl.textContent = txt.length > 85 ? txt.slice(0, 82) + '…' : txt;
    }
  }

  // Mini progress bar
  if (typeof s.progress === 'number') {
    const progEl = document.getElementById('bc-prog-' + id);
    if (progEl) {
      progEl.style.display = 'block';
      const fill = document.getElementById('bc-prog-fill-' + id);
      if (fill) fill.style.width = Math.round(s.progress * 100) + '%';
    }
  }

  // "Download All" button — appears once done
  if (s.status === 'done') {
    const actEl = document.getElementById('bc-actions-' + id);
    if (actEl && actEl.children.length === 0) {
      const files = s.doneFiles && s.doneFiles.length ? s.doneFiles : s.files.map(f => f.filename);
      if (files.length > 0) {
        const btn = document.createElement('button');
        btn.className   = 'btn-sm bc-dl-btn';
        btn.textContent = '↓ Download All (' + files.length + ')';
        btn.onclick     = () => downloadAll(id, files);
        actEl.appendChild(btn);
        actEl.style.display = 'flex';
      }
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

  const el = document.createElement('div');
  el.className = 'file-item' + (f.chapter === 0 ? ' full' : '');
  el.style.animationDelay = (_fileIdx++ * 0.07) + 's';
  el.innerHTML =
    '<div>' +
      '<div class="fi-name">' + icon + ' ' + esc(f.filename) + '</div>' +
      '<div class="fi-meta">' + esc(f.title) + ' · ' + dur + '</div>' +
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

  // Restore the single-book out-grid structure
  const container = document.querySelector('.batch-grid, .out-grid');
  if (container) {
    container.className = 'out-grid';
    container.innerHTML =
      '<div class="card">' +
        '<div class="log-hdr">' +
          '<div class="sect" style="margin:0">Live Log</div>' +
          '<button class="btn-sm" onclick="clearLog()">Clear</button>' +
        '</div>' +
        '<div class="log-box" id="log"></div>' +
      '</div>' +
      '<div class="card">' +
        '<div class="sect">Output Files</div>' +
        '<div class="file-list" id="file-list">' +
          '<div class="empty"><p>Files appear here as each chapter is processed.</p></div>' +
        '</div>' +
      '</div>';
  }
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

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3200);
}
