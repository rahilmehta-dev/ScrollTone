// ─── State ────────────────────────────────────────────────────────────────────
let jobId    = null;
let es       = null;
let _fileIdx = 0;  // for staggered file-item entrance animation

// ─── File drag & drop ─────────────────────────────────────────────────────────
const dz  = document.getElementById('drop-zone');
const inp = document.getElementById('epub-file');

inp.addEventListener('change', e => e.target.files[0] && showFile(e.target.files[0]));

dz.addEventListener('dragover',  e => { e.preventDefault(); dz.classList.add('drag-over'); });
dz.addEventListener('dragleave', () => dz.classList.remove('drag-over'));
dz.addEventListener('drop', e => {
  e.preventDefault(); dz.classList.remove('drag-over');
  const f = e.dataTransfer.files[0];
  if (f && f.name.toLowerCase().endsWith('.epub')) {
    inp.files = e.dataTransfer.files; showFile(f);
    dz.classList.add('drop-flash');
    dz.addEventListener('animationend', () => dz.classList.remove('drop-flash'), {once:true});
  } else {
    toast('Please drop an .epub file');
  }
});

function showFile(f) {
  dz.classList.add('has-file');
  const n = document.getElementById('up-name');
  n.textContent = f.name + '  (' + fmtBytes(f.size) + ')';
  n.style.display = 'block';
  dz.querySelector('.up-title').textContent = 'EPUB loaded';
  dz.querySelector('.up-sub').textContent   = 'Click to replace';
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
  const file = inp.files[0];
  if (!file) { toast('Please select an EPUB file first'); return; }

  const fd = new FormData();
  fd.append('file',          file);
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
    jobId = d.job_id;
    connectSSE(jobId);
  } catch (e) {
    setDot('error'); setStatus('Error');
    addLog('Error: ' + e.message, 'err');
    resetBtns();
  }
}

// ─── SSE ──────────────────────────────────────────────────────────────────────
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

// ─── Stop ─────────────────────────────────────────────────────────────────────
function stopJob() {
  if (!jobId) return;
  fetch('/stop/' + jobId, { method: 'POST' });
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
  _fileIdx = 0;
  document.getElementById('log').innerHTML = '';
  document.getElementById('file-list').innerHTML =
    '<div class="empty">' +
    '<p>Files appear here as each chapter is processed.</p></div>';
  setProg(0, '');
}

function resetBtns() {
  const b = document.getElementById('start-btn');
  b.disabled = false; b.textContent = 'Start Converting';
  const s = document.getElementById('stop-btn');
  s.style.display = 'none'; s.disabled = false;
}

function clearLog() { document.getElementById('log').innerHTML = ''; }

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
