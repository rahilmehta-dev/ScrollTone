// ─── State ────────────────────────────────────────────────────────────────────
let jobId       = null;
let eventSource = null;
let _fileIdx    = 0;  // for staggered file-item entrance animation
let _jobRunning = false;  // true while any conversion is in progress

// Batch state (multi-EPUB)
let batchJobIds = [];
let batchSources = {};
let batchState   = {};

// ─── Shared preview sample text ────────────────────────────────────────────────
// Fetched once from the backend (backend/voices.py PREVIEW_JOB_TEXT) so the UI
// never has its own copy to drift out of sync with what's actually spoken.
let _sampleText = '';
fetch('/api/sample-text').then(r => r.json()).then(d => { _sampleText = d.text || ''; }).catch(() => {});

// Can this browser open a native save-folder picker itself? Chrome/Edge/Brave
// only (Firefox/Safari don't implement the File System Access API).
const _fsAccessSupported = 'showDirectoryPicker' in window;

// ─── Docker detection ─────────────────────────────────────────────────────────
let _cpuCount = 4;
fetch('/api/config').then(r => r.json()).then(d => {
  if (d.cpu_count) {
    _cpuCount = d.cpu_count;
    const workersInput = document.getElementById('chatterbox_workers');
    if (workersInput) workersInput.max = String(_cpuCount);
    const hint = document.getElementById('chatterbox-workers-hint');
    if (hint) hint.textContent = 'This machine has ' + _cpuCount + ' CPU cores. ~6-7GB RAM per worker — keep worker count within what your RAM can hold.';

    const kokoroWorkersInput = document.getElementById('kokoro_workers');
    if (kokoroWorkersInput) kokoroWorkersInput.max = String(_cpuCount);
    const kokoroHint = document.getElementById('kokoro-workers-hint');
    if (kokoroHint) kokoroHint.textContent = 'This machine has ' + _cpuCount + ' CPU cores. ~1.5GB RAM per worker — keep worker count within what your RAM can hold.';
  }
  // Processing Device — only offer options this server can actually use
  // (e.g. MPS never applies inside a Linux Docker container).
  if (d.devices && d.devices.length) {
    const DEVICE_LABELS = {
      auto: 'Auto — best available',
      cpu:  'CPU only',
      cuda: 'CUDA GPU',
      mps:  'Apple Silicon (MPS)',
    };
    const deviceSel = document.getElementById('device');
    if (deviceSel) {
      deviceSel.innerHTML = d.devices.map(v =>
        `<option value="${v}"${v === 'auto' ? ' selected' : ''}>${DEVICE_LABELS[v] || v}</option>`
      ).join('');
    }
  }
  if (!d.docker) return;
  // The container has no GUI for a native OS dialog, and — more fundamentally
  // — can't write to an arbitrary folder on your device at all (it can only
  // see what's bind-mounted). The browser itself can, though: it's running on
  // your actual device. Swap the folder button over to the File System Access
  // API, which opens a real OS folder picker in the browser and hands us a
  // handle we can write each finished file into as it completes.
  const btn = document.getElementById('folder-btn');
  const hint = document.getElementById('out-dir-hint');
  if (_fsAccessSupported) {
    if (btn) { btn.removeAttribute('onclick'); btn.addEventListener('click', chooseDeviceFolder); }
    if (hint) hint.textContent = 'Click the folder button to choose a folder on this device — finished files are saved there automatically as they complete.';
  } else {
    if (btn) { btn.disabled = true; btn.title = 'Choosing a save folder needs Chrome, Edge, or Brave'; }
    if (hint) hint.textContent = 'Choosing a save folder needs Chrome, Edge, or Brave. Finished files will also appear below to download individually.';
  }
  // In Docker, localhost = the container. Ollama runs on the host, so use host.docker.internal.
  const ollamaInp = document.getElementById('ollama_url');
  if (ollamaInp && ollamaInp.value.includes('localhost')) {
    ollamaInp.value = 'http://host.docker.internal:11434';
  }
}).catch(() => {});

// ─── File drag & drop ─────────────────────────────────────────────────────────
const dropZone  = document.getElementById('drop-zone');
const fileInput = document.getElementById('epub-file');

fileInput.addEventListener('change', e => {
  if (e.target.files.length) showFiles(e.target.files);
});

dropZone.addEventListener('dragover',  e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', e => {
  e.preventDefault(); dropZone.classList.remove('drag-over');
  const all   = Array.from(e.dataTransfer.files);
  const books = all.filter(f => /\.(epub|txt)$/i.test(f.name));
  if (books.length) {
    fileInput.files = e.dataTransfer.files;
    showFiles(e.dataTransfer.files);
    dropZone.classList.add('drop-flash');
    dropZone.addEventListener('animationend', () => dropZone.classList.remove('drop-flash'), {once:true});
  } else {
    toast('Please drop .epub or .txt files only');
  }
});

function showFiles(fileList) {
  dropZone.classList.add('has-file');
  const n = document.getElementById('up-name');
  if (fileList.length === 1) {
    const f = fileList[0];
    n.textContent = f.name + '  (' + fmtBytes(f.size) + ')';
    dropZone.querySelector('.up-title').textContent = 'Book loaded';
    dropZone.querySelector('.up-sub').textContent   = 'Click to replace';
    _showCoverStrip(f.name);
    fetchChapters(f);
  } else {
    const total = Array.from(fileList).reduce((s, f) => s + f.size, 0);
    n.textContent = fileList.length + ' books selected  (' + fmtBytes(total) + ')';
    dropZone.querySelector('.up-title').textContent = fileList.length + ' books loaded';
    dropZone.querySelector('.up-sub').textContent   = 'Click to change selection';
    // Hide chapter card for multi-book batch
    document.getElementById('chapter-card').style.display = 'none';
    document.getElementById('cover-strip').style.display = 'none';
    _resetAtmosphere(); // no single cover to take the room's color from
    _chaptersData = [];
  }
  n.style.display = 'block';
}

// ─── Cover strip ────────────────────────────────────────────────────────────
// Shows immediately with a filename-based title as an optimistic placeholder,
// then _applyCoverData() (called once /api/chapters resolves) fills in the
// real title/author/cover art the backend already extracts for MP3 tagging.
function _showCoverStrip(filename) {
  const strip = document.getElementById('cover-strip');
  const img   = document.getElementById('cover-img');
  const fallback = document.getElementById('cover-thumb-fallback');
  img.style.display = 'none';
  fallback.style.display = '';
  document.getElementById('cover-strip-title').textContent = filename.replace(/\.(epub|txt)$/i, '');
  document.getElementById('cover-strip-author').textContent = '';
  strip.style.display = 'flex';
  _resetAtmosphere(); // back to the default room color until a real cover loads
}

function _applyCoverData(d) {
  if (d.title) document.getElementById('cover-strip-title').textContent = d.title;
  if (d.author) document.getElementById('cover-strip-author').textContent = d.author;
  if (d.cover && d.cover.data) {
    const img = document.getElementById('cover-img');
    img.onload = () => _updateAtmosphereFromCover(img);
    img.src = 'data:' + d.cover.mime + ';base64,' + d.cover.data;
    img.style.display = '';
    document.getElementById('cover-thumb-fallback').style.display = 'none';
  } else {
    _resetAtmosphere(); // .txt upload, or an EPUB with no embedded cover
  }
}

// ─── Atmosphere — the room takes its color from whatever book is loaded ───────
// Same idea as Apple Music's Now Playing background: sample the cover on an
// offscreen canvas (cheap — downsampled to 48x48 first) and turn its most
// prominent hue(s) into the glow behind the whole page. Raw photo pixels are
// rarely vivid enough to use directly, so only the *hue* is taken from the
// image; saturation/lightness are pinned to values already tuned to look
// good against the glass panels and text, regardless of source image.
function _setAtmosphere(hueA, hueB) {
  const root = document.documentElement.style;
  root.setProperty('--atmo-a', 'hsl(' + hueA + ' 70% 58%)');
  root.setProperty('--atmo-b', 'hsl(' + hueB + ' 55% 42%)');
  root.setProperty('--atmo-c', 'hsl(' + hueA + ' 35% 16%)');
  // Buttons, toggles, and badges don't crossfade the way the backdrop does —
  // they're small, high-contrast surfaces where an instant color swap reads
  // as "updated," not "broken," so a single --hue-a write is enough: every
  // --accent* token in style.css is a hsl(var(--hue-a) ...) formula, not a
  // literal, and recomputes on its own the moment this changes.
  root.setProperty('--hue-a', hueA);
  root.setProperty('--hue-b', hueB);
}

function _resetAtmosphere() {
  const root = document.documentElement.style;
  root.removeProperty('--atmo-a');
  root.removeProperty('--atmo-b');
  root.removeProperty('--atmo-c');
  root.removeProperty('--hue-a');
  root.removeProperty('--hue-b');
}

function _updateAtmosphereFromCover(imgEl) {
  try {
    const size = 48;
    const canvas = document.createElement('canvas');
    canvas.width = size; canvas.height = size;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(imgEl, 0, 0, size, size);
    const { data } = ctx.getImageData(0, 0, size, size);

    // Bucket pixels into 24 hue buckets (15° each), weighted by saturation
    // so vivid pixels count more than washed-out ones. Near-grey/near-black/
    // near-white pixels carry no usable hue and are skipped entirely.
    const buckets = new Array(24).fill(0);
    let vividCount = 0, total = 0;
    for (let i = 0; i < data.length; i += 4) {
      total++;
      const r = data[i] / 255, g = data[i + 1] / 255, b = data[i + 2] / 255;
      const max = Math.max(r, g, b), min = Math.min(r, g, b);
      const l = (max + min) / 2, d = max - min;
      if (d < 0.06 || l < 0.08 || l > 0.94) continue;
      const s = d / (1 - Math.abs(2 * l - 1));
      if (s < 0.15) continue;
      let h;
      if (max === r) h = ((g - b) / d) % 6;
      else if (max === g) h = (b - r) / d + 2;
      else h = (r - g) / d + 4;
      h = Math.round(h * 60);
      if (h < 0) h += 360;
      buckets[Math.floor(h / 15) % 24] += s;
      vividCount++;
    }

    // A mostly black-and-white or monochrome cover has no real hue signal —
    // fall back to the default room color instead of a meaningless one.
    if (vividCount < total * 0.06) { _resetAtmosphere(); return; }

    let primaryIdx = 0;
    for (let i = 1; i < 24; i++) if (buckets[i] > buckets[primaryIdx]) primaryIdx = i;
    // Secondary hue = the strongest bucket at least 60° away from the
    // primary, so the two glows read as genuinely different colors instead
    // of near-duplicates of the same hue.
    let secondaryIdx = -1;
    for (let i = 0; i < 24; i++) {
      if (buckets[i] <= 0) continue; // bucket has no actual pixels — not a real candidate
      const dist = Math.min(Math.abs(i - primaryIdx), 24 - Math.abs(i - primaryIdx));
      if (dist >= 4 && (secondaryIdx === -1 || buckets[i] > buckets[secondaryIdx])) secondaryIdx = i;
    }
    const hueA = primaryIdx * 15 + 7;
    const hueB = secondaryIdx === -1 ? (hueA + 45) % 360 : secondaryIdx * 15 + 7;
    _setAtmosphere(hueA, hueB);
  } catch (e) {
    console.warn('Cover color extraction skipped:', e);
  }
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
    const r = await fetch('/api/chapters', { method: 'POST', body: fd });
    if (!r.ok) {
      let detail = '';
      try { detail = (await r.json()).detail || ''; } catch(_) {}
      throw new Error('HTTP ' + r.status + (detail ? ': ' + detail : ''));
    }
    const d = await r.json();
    _chaptersData = d.chapters;
    _renderChapters(d.chapters);
    _applyCoverData(d);
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

  // Desktop: switch from the full-width Book/Voice & Model pages to the
  // two-column layout with the Output panel now visible alongside them.
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

  // Desktop: once a job has started, keep the Output panel around (it may
  // still be running) rather than collapsing back to the full-width layout.
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
    const r = await fetch('/api/pick-folder');
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

// ─── Save to a folder on this device (Docker) ─────────────────────────────────
// The Docker container can't write to an arbitrary host path — it only ever
// sees whatever's bind-mounted. The browser can, though, since it's running on
// your actual device: the File System Access API opens a real OS folder
// picker there and hands back a handle we can write into directly. Once set,
// each finished file is fetched from the server and written into that folder
// as the conversion produces it — no server-side path involved at all.
let _deviceDirHandle = null;

async function chooseDeviceFolder() {
  if (!_fsAccessSupported) {
    toast('Choosing a save folder needs Chrome, Edge, or Brave');
    return;
  }
  try {
    _deviceDirHandle = await window.showDirectoryPicker({ id: 'scrolltone-output', mode: 'readwrite' });
  } catch (e) {
    return; // user cancelled the picker
  }
  document.getElementById('out_dir').value = _deviceDirHandle.name;
  const status = document.getElementById('device-folder-status');
  status.textContent = 'Finished files will be saved into "' + _deviceDirHandle.name + '" on this device automatically.';
  status.style.display = 'block';
  document.getElementById('clear-device-folder-btn').style.display = 'inline-flex';
}

function clearDeviceFolder() {
  _deviceDirHandle = null;
  document.getElementById('out_dir').value = '';
  const status = document.getElementById('device-folder-status');
  status.textContent = ''; status.style.display = 'none';
  document.getElementById('clear-device-folder-btn').style.display = 'none';
}

async function saveFileToDevice(forJobId, filename) {
  if (!_deviceDirHandle) return false;
  try {
    const resp = await fetch('/api/download/' + forJobId + '/' + encodeURIComponent(filename));
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const blob = await resp.blob();
    const fileHandle = await _deviceDirHandle.getFileHandle(filename, { create: true });
    const writable   = await fileHandle.createWritable();
    await writable.write(blob);
    await writable.close();
    return true;
  } catch (e) {
    console.error('saveFileToDevice failed:', e);
    return false;
  }
}

// ─── Advanced toggle ──────────────────────────────────────────────────────────
function toggleAdv() {
  document.getElementById('adv-btn').classList.toggle('open');
  document.getElementById('adv-body').classList.toggle('open');
}

// ─── Multi-voice / ambience toggles ───────────────────────────────────────────
// Both features use the same local Ollama LLM, so the settings block stays
// visible if either is enabled.
function toggleMultiVoice() { updateLlmSettingsVisibility(); }
function toggleAmbience()   { updateLlmSettingsVisibility(); }
function updateLlmSettingsVisibility() {
  const enabled = document.getElementById('multi_voice').checked
                || document.getElementById('ambience').checked;
  document.getElementById('mv-settings').style.display = enabled ? 'block' : 'none';
}

// ─── TTS Engine toggle ────────────────────────────────────────────────────────
const ENGINE_WARNINGS = {
  higgs: 'Uses ~12GB RAM and runs roughly at realtime speed. Licensed under Boson AI\'s Community License (not Apache/MIT) — requires attribution and a commercial license above 100k annual active users.',
  chatterbox: 'Uses your Device setting. CPU is slow (~6x the audiobook\'s runtime) but stable. MPS (Apple Silicon GPU) has a confirmed memory leak that can spike past 70GB+ RAM inside a single chunk\'s generation — CPU is strongly recommended unless you know that risk and are watching memory closely.',
};

// Plain-language voice-mode cards proxy the real #engine select: they set
// its value and fire a native 'change' event so toggleEngine() (bound via
// the select's own onchange) runs exactly as if the user had picked from
// the dropdown. Keeps the select as the single source of truth.
//
// "Use a Voice" always means Kokoro. "Clone a Voice" means one of the two
// cloning engines — whichever was last selected (defaulting to Higgs,
// since it doesn't carry Chatterbox's MPS memory-leak risk) — with the
// actual engine name tucked behind the small "Advanced" picker below,
// never shown as the primary choice.
const DEFAULT_CLONE_ENGINE = 'higgs';

function pickVoiceMode(mode) {
  const sel = document.getElementById('engine');
  if (mode === 'existing') {
    if (sel.value === 'kokoro') return;
    sel.value = 'kokoro';
  } else {
    if (sel.value !== 'kokoro') return; // already on a cloning engine
    sel.value = DEFAULT_CLONE_ENGINE;
  }
  sel.dispatchEvent(new Event('change'));
}

// The rare override for someone who wants to pick Higgs vs. Chatterbox
// specifically, reached only via the small "Advanced" link in the clone panel.
function pickCloneModel(name) {
  const sel = document.getElementById('engine');
  if (sel.value === name) return;
  sel.value = name;
  sel.dispatchEvent(new Event('change'));
}

function toggleCloneModelPicker() {
  const picker = document.getElementById('clone-model-picker');
  picker.style.display = picker.style.display === 'none' ? 'flex' : 'none';
}

function toggleEngine() {
  const engine = document.getElementById('engine').value;
  const isKokoro = engine === 'kokoro';

  const existingBtn = document.getElementById('vc-existing-btn');
  const cloneBtn = document.getElementById('vc-clone-btn');
  existingBtn.classList.toggle('active', isKokoro);
  existingBtn.setAttribute('aria-pressed', isKokoro ? 'true' : 'false');
  cloneBtn.classList.toggle('active', !isKokoro);
  cloneBtn.setAttribute('aria-pressed', !isKokoro ? 'true' : 'false');
  document.querySelectorAll('.mini-seg-opt').forEach(b => {
    b.classList.toggle('active', b.dataset.model === engine);
  });

  document.getElementById('kokoro-voice-field').style.display = isKokoro ? '' : 'none';
  document.getElementById('engine-settings').style.display = isKokoro ? 'none' : 'block';
  document.getElementById('engine-warning').textContent = ENGINE_WARNINGS[engine] || '';
  document.getElementById('kokoro-workers-field').style.display = isKokoro ? 'block' : 'none';
  const workersField = document.getElementById('chatterbox-workers-field');
  const speedField   = document.getElementById('chatterbox-speed-field');
  const cfgField      = document.getElementById('chatterbox-cfg-field');
  const exagField     = document.getElementById('chatterbox-exaggeration-field');
  const tempField      = document.getElementById('chatterbox-temperature-field');
  const breathsField   = document.getElementById('chatterbox-breaths-field');
  const showWorkers  = engine === 'chatterbox';
  workersField.style.display = showWorkers ? 'block' : 'none';
  speedField.style.display   = showWorkers ? 'block' : 'none';
  cfgField.style.display     = showWorkers ? 'block' : 'none';
  exagField.style.display    = showWorkers ? 'block' : 'none';
  tempField.style.display    = showWorkers ? 'block' : 'none';
  breathsField.style.display = showWorkers ? 'block' : 'none';
  const showHiggsSampling = engine === 'higgs';
  document.getElementById('higgs-temperature-field').style.display = showHiggsSampling ? 'block' : 'none';
  document.getElementById('higgs-top-p-field').style.display       = showHiggsSampling ? 'block' : 'none';
  document.getElementById('higgs-top-k-field').style.display       = showHiggsSampling ? 'block' : 'none';
  document.getElementById('higgs-workers-field').style.display     = showHiggsSampling ? 'block' : 'none';

  if (showWorkers || showHiggsSampling) {
    const advBtn  = document.getElementById('adv-btn');
    const advBody = document.getElementById('adv-body');
    if (advBtn && !advBtn.classList.contains('open')) { advBtn.classList.add('open'); advBody.classList.add('open'); }
  }

  const multiVoice = document.getElementById('multi_voice');
  multiVoice.disabled = !isKokoro;
  if (!isKokoro) multiVoice.checked = false;
  multiVoice.closest('label').style.opacity = isKokoro ? '' : '.45';
  multiVoice.closest('label').title = isKokoro ? '' : 'Multi-voice is Kokoro-only for now';
  updateLlmSettingsVisibility();

  // Speech Speed only does anything for Kokoro (Chatterbox has its own
  // working "Chatterbox Speed" field above; Higgs has no rate control at all).
  document.getElementById('speed-field').style.display = isKokoro ? '' : 'none';

  // Transformer G2P is Kokoro's own phonemizer upgrade — Higgs/Chatterbox
  // never touch Kokoro's G2P pipeline, so the toggle would do nothing there.
  document.getElementById('trf-field').style.display = isKokoro ? '' : 'none';

  // Preview & Tweak — same card, wording/behavior swaps per engine. Switching
  // engines mid-preview makes whatever's in flight stale, so cancel it.
  stopSamplePreview();
  const previewBtn = document.getElementById('sample-preview-btn');
  if (previewBtn) previewBtn.textContent = isKokoro ? '▶ Preview Voice' : '▶ Preview Cloned Voice';
  const previewStatus = document.getElementById('sample-preview-status');
  const previewAudio  = document.getElementById('sample-preview-audio');
  if (previewStatus) previewStatus.textContent = '';
  if (previewAudio)  { previewAudio.pause(); previewAudio.style.display = 'none'; }
}

// ─── Preview & Tweak ────────────────────────────────────────────────────────────
// The "listen before you run" flow for every engine, backed by the same job/SSE
// machinery as a real conversion: POST /api/preview-job runs one synthetic
// "chapter" (backend/voices.py PREVIEW_JOB_TEXT) through the real ChapterProcessor
// (backend/pipeline.py run_preview_job), then GET /api/stream/{job_id} — the
// exact endpoint a real conversion streams from — reports real chunk-by-chunk
// progress and logs. That's also what makes Multi-voice/Ambient sound actually
// run here (same attribute_speakers()/detect_ambience_cues() calls), and lets
// Stop cancel the job server-side (POST /api/stop/{job_id}) instead of just
// abandoning a client-side fetch.
let _samplePreviewURL = null;
let _sampleJobId = null;
let _sampleEventSource = null;
let _sampleLastDesc = '';

function toggleSampleText() {
  const box = document.getElementById('sample-text-box');
  const btn = document.getElementById('sample-text-toggle');
  const showing = box.style.display !== 'none' && box.textContent;
  if (showing) {
    box.style.display = 'none';
    btn.textContent = 'Read the sample text';
  } else {
    box.textContent = _sampleText || 'Sample text unavailable.';
    box.style.display = 'block';
    btn.textContent = 'Hide the sample text';
  }
}

function _sampleLog(text, cls) {
  const log   = document.getElementById('sample-log');
  const empty = document.getElementById('sample-log-empty');
  if (empty) empty.remove();
  const line = document.createElement('div');
  line.className = 'sl-line' + (cls ? ' ' + cls : '');
  const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  line.textContent = '[' + time + '] ' + text;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

function _setSampleRunning(running) {
  document.getElementById('sample-stop-btn').style.display   = running ? 'inline-flex' : 'none';
  document.getElementById('sample-prog-track').style.display = running ? 'block' : 'none';
  const fill = document.getElementById('sample-prog-fill');
  if (running) {
    // Indeterminate until the job's first ch_start/ch_prog event arrives —
    // starting the pipeline (loading a model, etc.) has no chunk count yet.
    fill.classList.add('indeterminate');
    fill.style.width = '';
  } else {
    fill.classList.remove('indeterminate');
    fill.style.width = '0%';
  }
}

function _closeSampleStream() {
  if (_sampleEventSource) { _sampleEventSource.close(); _sampleEventSource = null; }
}

function stopSamplePreview() {
  if (!_sampleJobId) return;
  const jobId = _sampleJobId;
  _sampleJobId = null;
  fetch('/api/stop/' + jobId, { method: 'POST' }).catch(() => {});
  _closeSampleStream();
  document.getElementById('sample-preview-status').textContent = 'Stopped.';
  _sampleLog('Stopped — ' + _sampleLastDesc, 'sl-stop');
  _setSampleRunning(false);
}

async function previewSample() {
  const engine = document.getElementById('engine').value;
  const status = document.getElementById('sample-preview-status');
  const audio  = document.getElementById('sample-preview-audio');

  const fd = new FormData();
  fd.append('engine', engine);
  let desc = engine;

  if (engine === 'kokoro') {
    const voice = document.getElementById('voice').value;
    const speed = document.getElementById('speed').value;
    fd.append('voice', voice);
    fd.append('speed', speed);
    desc = 'kokoro · voice=' + voice + ' · speed=' + speed + '×';

    // Multi-voice/Ambient sound apply to the preview too, exactly as they
    // will to the real book — same attribute_speakers()/detect_ambience_
    // cues() LLM calls the conversion uses (backend/pipeline.py run_preview_job).
    const multiVoice = document.getElementById('multi_voice').checked;
    const ambience   = document.getElementById('ambience').checked;
    if (multiVoice || ambience) {
      const ollamaUrl   = document.getElementById('ollama_url').value.trim() || 'http://localhost:11434';
      const ollamaModel = document.getElementById('ollama_model').value;
      fd.append('ollama_url', ollamaUrl);
      fd.append('ollama_model', ollamaModel);
      desc += ' · model=' + ollamaModel;
    }
    if (multiVoice) { fd.append('multi_voice', 'true'); desc += ' · multi-voice=on'; }
    if (ambience)   { fd.append('ambience', 'true');    desc += ' · ambience=on'; }
    const kWorkers = document.getElementById('kokoro_workers').value || '1';
    if (kWorkers > 1) { fd.append('kokoro_workers', kWorkers); desc += ' · workers=' + kWorkers; }
  } else {
    const refInput = document.getElementById('reference_audio');
    if (!refInput.files || !refInput.files.length) {
      toast('Upload a reference voice clip first'); return;
    }
    fd.append('reference_audio', refInput.files[0]);
    // Send the raw dropdown value (including 'auto') and let the backend
    // resolve it, same as the main Convert form (app.js line ~728) — the
    // /api/preview-job route already does proper mps/cuda/cpu detection
    // (backend/routes/preview.py). Forcing 'cpu' here used to silently run
    // both engines on CPU instead of MPS/CUDA even when the user picked
    // Auto — for Higgs that's just unnecessarily slow; for Chatterbox, MPS
    // carries a confirmed memory-leak risk (see ENGINE_WARNINGS above), so
    // whichever device this resolves to is now a real, visible choice.
    const device = document.getElementById('device').value;
    fd.append('device', device);
    desc += ' · device=' + device;

    if (engine === 'chatterbox') {
      const cSpeed = document.getElementById('chatterbox_speed').value || '1.0';
      const cCfg   = document.getElementById('chatterbox_cfg_weight').value || '0.3';
      const cExag  = document.getElementById('chatterbox_exaggeration').value || '0.7';
      const cTemp  = document.getElementById('chatterbox_temperature').value || '0.8';
      const cWorkers = document.getElementById('chatterbox_workers').value || '1';
      fd.append('chatterbox_speed', cSpeed);
      fd.append('chatterbox_cfg_weight',   cCfg);
      fd.append('chatterbox_exaggeration', cExag);
      fd.append('chatterbox_temperature',  cTemp);
      fd.append('chatterbox_workers', cWorkers);
      desc += ' · speed=' + cSpeed + ' · cfg=' + cCfg + ' · exaggeration=' + cExag + ' · temperature=' + cTemp;
      if (cWorkers > 1) desc += ' · workers=' + cWorkers;
    }
    if (engine === 'higgs') {
      const hTemp = document.getElementById('higgs_temperature').value || '0.15';
      const hTopP = document.getElementById('higgs_top_p').value || '0.75';
      const hTopK = document.getElementById('higgs_top_k').value || '25';
      const hWorkers = document.getElementById('higgs_workers').value || '1';
      fd.append('higgs_temperature', hTemp);
      fd.append('higgs_top_p', hTopP);
      fd.append('higgs_top_k', hTopK);
      fd.append('higgs_workers', hWorkers);
      desc += ' · temperature=' + hTemp + ' · top_p=' + hTopP + ' · top_k=' + hTopK;
      if (hWorkers > 1) desc += ' · workers=' + hWorkers;
    }

    // Ambient sound applies regardless of engine in the real conversion (see
    // backend/chapter_processor.py), so it's tested here too. Multi-voice
    // stays Kokoro-only — the checkbox is disabled for other engines already.
    const ambience = document.getElementById('ambience').checked;
    if (ambience) {
      const ollamaUrl   = document.getElementById('ollama_url').value.trim() || 'http://localhost:11434';
      const ollamaModel = document.getElementById('ollama_model').value;
      fd.append('ambience', 'true');
      fd.append('ollama_url', ollamaUrl);
      fd.append('ollama_model', ollamaModel);
      desc += ' · ambience=on · model=' + ollamaModel;
    }
  }

  // Starting a new preview cancels whatever's still running server-side too
  // — changing a parameter and hitting Preview again doesn't mean waiting
  // for the old job first. This also logs the old run as "Stopped".
  stopSamplePreview();

  _sampleLastDesc = desc;
  _setSampleRunning(true);
  status.textContent = 'Starting…';
  audio.style.display = 'none';
  _sampleLog('Started — ' + desc, 'sl-start');

  let jobId;
  try {
    const r = await fetch('/api/preview-job', { method: 'POST', body: fd });
    if (!r.ok) {
      let detail = '';
      try { detail = (await r.json()).detail || ''; } catch(_) {}
      throw new Error(detail || ('HTTP ' + r.status));
    }
    const d = await r.json();
    jobId = d.job_id;
    if (d.reference_warnings && d.reference_warnings.length) {
      toast('Reference clip quality warning — see the log below');
      d.reference_warnings.forEach(w => _sampleLog('⚠ ' + w, 'sl-err'));
    }
  } catch (e) {
    status.textContent = 'Preview failed: ' + e.message;
    _sampleLog('Failed — ' + desc + ': ' + e.message, 'sl-err');
    _setSampleRunning(false);
    return;
  }

  _sampleJobId = jobId;
  _connectSampleStream(jobId, desc);
}

function _connectSampleStream(jobId, desc) {
  const status = document.getElementById('sample-preview-status');
  const audio  = document.getElementById('sample-preview-audio');
  const fill   = document.getElementById('sample-prog-fill');
  let totalChunks = 0;

  _closeSampleStream();
  const es = new EventSource('/api/stream/' + jobId);
  _sampleEventSource = es;

  es.onmessage = e => {
    let m;
    try { m = JSON.parse(e.data); } catch(_) { return; }

    if (m.type === 'log') {
      const line = m.msg.trim();
      if (line) _sampleLog(line);
    } else if (m.type === 'status') {
      status.textContent = m.msg;
    } else if (m.type === 'ch_start') {
      totalChunks = m.chunks || 0;
      fill.classList.remove('indeterminate');
      fill.style.width = '0%';
      status.textContent = totalChunks ? 'Synthesizing — 0/' + totalChunks + ' chunks…' : 'Synthesizing…';
    } else if (m.type === 'ch_prog') {
      const pct = Math.round((m.pct || 0) * 100);
      fill.style.width = pct + '%';
      const doneChunks = totalChunks ? Math.round((m.pct || 0) * totalChunks) : 0;
      status.textContent = totalChunks
        ? 'Synthesizing — ' + doneChunks + '/' + totalChunks + ' chunks (' + pct + '%)…'
        : 'Synthesizing — ' + pct + '%…';
    } else if (m.type === 'file') {
      fetch('/api/download/' + jobId + '/' + encodeURIComponent(m.filename))
        .then(r => r.ok ? r.blob() : Promise.reject(new Error('HTTP ' + r.status)))
        .then(blob => {
          if (_samplePreviewURL) URL.revokeObjectURL(_samplePreviewURL);
          _samplePreviewURL = URL.createObjectURL(blob);
          audio.src = _samplePreviewURL;
          audio.style.display = '';
          audio.play().catch(() => {});
        })
        .catch(err => _sampleLog('Failed to fetch finished audio: ' + err.message, 'sl-err'));
    } else if (m.type === 'done') {
      _closeSampleStream();
      _sampleJobId = null;
      status.textContent = 'Playing the shared sample.';
      _sampleLog('Done — ' + desc, 'sl-done');
      _setSampleRunning(false);
    }
  };

  es.onerror = () => {
    // Stop() already closes the stream and logs "Stopped" itself, so only
    // treat this as a real failure if we're still tracking this job.
    if (_sampleJobId === jobId) {
      _sampleJobId = null;
      status.textContent = 'Preview failed: connection lost.';
      _sampleLog('Failed — ' + desc + ': connection lost', 'sl-err');
    }
    _closeSampleStream();
    _setSampleRunning(false);
  };
}

// ─── Auto-tune this voice ───────────────────────────────────────────────────────
// Opt-in (button, not automatic on upload): samples a handful of Chatterbox/
// Higgs parameter combos via Latin Hypercube Sampling, synthesizes the same
// short sample text with each against the uploaded reference clip in
// parallel, scores every candidate with DNSMOS (backend/voice_tuning.py),
// and reports the ranked results. Same job/SSE machinery as Preview & Tweak.
let _autotuneJobId = null;
let _autotuneEventSource = null;
let _autotuneLiveCandidates = [];   // accumulated as autotune_candidate_result messages arrive

function _autotuneLog(text, cls) {
  const log     = document.getElementById('autotune-log');
  const wrap    = document.getElementById('autotune-log-wrap');
  wrap.style.display = 'block';
  const line = document.createElement('div');
  line.className = 'sl-line' + (cls ? ' ' + cls : '');
  const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  line.textContent = '[' + time + '] ' + text;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

function _setAutotuneRunning(running) {
  document.getElementById('autotune-btn').disabled = running;
  document.getElementById('autotune-stop-btn').style.display = running ? 'inline-flex' : 'none';
  document.getElementById('autotune-prog-track').style.display = running ? 'block' : 'none';
  if (running) {
    document.getElementById('autotune-results').style.display = 'none';
    _autotuneLiveCandidates = [];
  }
}

async function startAutotune() {
  const engine = document.getElementById('engine').value;
  if (engine !== 'chatterbox' && engine !== 'higgs') {
    toast('Auto-tune only applies to Chatterbox/Higgs — switch engine first'); return;
  }
  const refInput = document.getElementById('reference_audio');
  if (!refInput.files || !refInput.files.length) {
    toast('Upload a reference voice clip first'); return;
  }

  const fd = new FormData();
  fd.append('engine', engine);
  fd.append('reference_audio', refInput.files[0]);
  let device = document.getElementById('device').value;
  fd.append('device', device);
  if (engine === 'chatterbox') {
    fd.append('chatterbox_workers', document.getElementById('chatterbox_workers').value || '1');
  } else {
    fd.append('higgs_workers', document.getElementById('higgs_workers').value || '1');
  }

  _setAutotuneRunning(true);
  document.getElementById('autotune-status').textContent = 'Starting…';
  _autotuneLog('Started — ' + engine + ' · device=' + device, 'sl-start');

  let jobId;
  try {
    const r = await fetch('/api/autotune-job', { method: 'POST', body: fd });
    if (!r.ok) {
      let detail = '';
      try { detail = (await r.json()).detail || ''; } catch(_) {}
      throw new Error(detail || ('HTTP ' + r.status));
    }
    const d = await r.json();
    jobId = d.job_id;
  } catch (e) {
    document.getElementById('autotune-status').textContent = 'Auto-tune failed: ' + e.message;
    _autotuneLog('Failed: ' + e.message, 'sl-err');
    _setAutotuneRunning(false);
    return;
  }

  _autotuneJobId = jobId;
  _connectAutotuneStream(jobId, engine);
}

function stopAutotune() {
  if (!_autotuneJobId) return;
  const jobId = _autotuneJobId;
  _autotuneJobId = null;
  fetch('/api/stop/' + jobId, { method: 'POST' }).catch(() => {});
  if (_autotuneEventSource) { _autotuneEventSource.close(); _autotuneEventSource = null; }
  document.getElementById('autotune-status').textContent = 'Stopped.';
  _autotuneLog('Stopped by user', 'sl-stop');
  _setAutotuneRunning(false);
}

function _connectAutotuneStream(jobId, engine) {
  const status = document.getElementById('autotune-status');
  const fill   = document.getElementById('autotune-prog-fill');
  let totalCandidates = 0;

  if (_autotuneEventSource) _autotuneEventSource.close();
  const es = new EventSource('/api/stream/' + jobId);
  _autotuneEventSource = es;

  es.onmessage = e => {
    let m;
    try { m = JSON.parse(e.data); } catch(_) { return; }

    if (m.type === 'log') {
      const line = m.msg.trim();
      if (line) _autotuneLog(line);
    } else if (m.type === 'status') {
      status.textContent = m.msg;
    } else if (m.type === 'ch_start') {
      totalCandidates = m.chunks || 0;
    } else if (m.type === 'ch_prog') {
      const pct = Math.round((m.pct || 0) * 100);
      const doneN = totalCandidates ? Math.round((m.pct || 0) * totalCandidates) : 0;
      status.textContent = totalCandidates
        ? 'Synthesizing — ' + doneN + '/' + totalCandidates + ' candidates (' + pct + '%)…'
        : 'Synthesizing — ' + pct + '%…';
    } else if (m.type === 'autotune_candidate_result') {
      _autotuneLiveCandidates.push({ params: m.params, score: m.score, filename: m.filename });
      _renderAutotuneCandidates(_autotuneLiveCandidates, m.best_score, jobId, engine, /*live=*/true);
      status.textContent = 'Synthesizing — ' + _autotuneLiveCandidates.length + '/' +
        (totalCandidates || '?') + ' candidates · best so far ' + m.best_score.toFixed(2) + '…';
    } else if (m.type === 'autotune_result') {
      _renderAutotuneCandidates(m.candidates, m.winner.score, jobId, engine, /*live=*/false);
      _applyAutotuneParams(m.winner.params, engine);
      toast('Auto-tune done — best-scoring settings applied (score ' + m.winner.score.toFixed(2) + ')');
    } else if (m.type === 'done') {
      if (_autotuneEventSource) { _autotuneEventSource.close(); _autotuneEventSource = null; }
      _autotuneJobId = null;
      status.textContent = 'Done.';
      _autotuneLog('Done', 'sl-done');
      _setAutotuneRunning(false);
    }
  };

  es.onerror = () => {
    if (_autotuneJobId === jobId) {
      _autotuneJobId = null;
      status.textContent = 'Auto-tune failed: connection lost.';
      _autotuneLog('Failed: connection lost', 'sl-err');
    }
    if (_autotuneEventSource) { _autotuneEventSource.close(); _autotuneEventSource = null; }
    _setAutotuneRunning(false);
  };
}

// Renders the results panel from whatever candidates have scored so far —
// called both incrementally (live=true, as each autotune_candidate_result
// streams in — the list grows and the ranking/BEST badge can reshuffle
// candidate to candidate) and once authoritatively at the end (live=false,
// from the final autotune_result, which also auto-fills the winner's
// settings). Sorts every call, so the DOM always reflects current ranking
// regardless of the (out-of-order, worker-dependent) arrival order.
function _renderAutotuneCandidates(candidates, bestScore, jobId, engine, live) {
  const box = document.getElementById('autotune-results');
  box.innerHTML = '';
  box.style.display = 'block';

  const sorted = [...candidates].sort((a, b) => b.score - a.score);

  const heading = document.createElement('div');
  heading.style.cssText = 'font-size:.73rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--txt-s);margin-bottom:8px';
  heading.textContent = live
    ? 'Results so far (' + sorted.length + ' scored, still running) — ranked by predicted naturalness'
    : 'Results — ranked by predicted naturalness';
  box.appendChild(heading);

  sorted.forEach((c, idx) => {
    const isBest = c.score === bestScore;
    const card = document.createElement('div');
    card.style.cssText = 'padding:10px 12px;border-radius:10px;margin-bottom:8px;background:rgba(255,255,255,.03);border:1px solid ' +
      (isBest ? 'var(--success)' : 'rgba(255,255,255,.06)');

    const paramStr = Object.entries(c.params).map(([k, v]) =>
      k + '=' + (typeof v === 'number' ? (Number.isInteger(v) ? v : v.toFixed(2)) : v)).join(' · ');

    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:8px;flex-wrap:wrap';
    row.innerHTML =
      '<span style="font-weight:700">#' + (idx + 1) + '</span>' +
      (isBest ? '<span style="font-size:.65rem;font-weight:800;letter-spacing:.06em;color:var(--success);background:rgba(52,211,153,.12);padding:2px 6px;border-radius:4px">BEST</span>' : '') +
      '<span style="font-size:.8rem;color:var(--txt-m)">score ' + c.score.toFixed(2) + '</span>' +
      '<span style="font-size:.73rem;color:var(--txt-s);font-family:monospace">' + paramStr + '</span>';
    card.appendChild(row);

    const audio = document.createElement('audio');
    audio.controls = true;
    audio.style.cssText = 'width:100%;height:32px;margin-top:6px';
    audio.src = '/api/download/' + jobId + '/' + encodeURIComponent(c.filename);
    card.appendChild(audio);

    const useBtn = document.createElement('button');
    useBtn.type = 'button';
    useBtn.className = 'btn-sm';
    useBtn.style.marginTop = '6px';
    useBtn.textContent = (isBest && !live) ? '✓ Use these settings (applied)' : 'Use these settings';
    useBtn.onclick = () => _applyAutotuneParams(c.params, engine);
    card.appendChild(useBtn);

    box.appendChild(card);
  });
}

function _applyAutotuneParams(params, engine) {
  if (engine === 'chatterbox') {
    if ('cfg_weight' in params) document.getElementById('chatterbox_cfg_weight').value = params.cfg_weight.toFixed(2);
    if ('temperature' in params) document.getElementById('chatterbox_temperature').value = params.temperature.toFixed(2);
  } else if (engine === 'higgs') {
    if ('temperature' in params) document.getElementById('higgs_temperature').value = params.temperature.toFixed(2);
    if ('top_p' in params) document.getElementById('higgs_top_p').value = params.top_p.toFixed(2);
    if ('top_k' in params) document.getElementById('higgs_top_k').value = Math.round(params.top_k);
  }
}

function showReferenceAudio() {
  const input  = document.getElementById('reference_audio');
  const status = document.getElementById('reference-audio-status');
  if (!input.files || !input.files.length) { status.textContent = ''; return; }
  const f = input.files[0];
  status.textContent = f.name + '  (' + fmtBytes(f.size) + ')';
}

// ─── Format toggle ────────────────────────────────────────────────────────────
function onFormatChange() {
  const fmt = document.querySelector('input[name="output_format"]:checked').value;
  document.getElementById('bitrate-row').classList.toggle('visible', fmt === 'mp3');
}

// ─── Start ────────────────────────────────────────────────────────────────────
async function startJob() {
  if (!fileInput.files || fileInput.files.length === 0) {
    toast('Please select at least one EPUB or TXT file'); return;
  }

  const engine = document.getElementById('engine').value;
  const refInput = document.getElementById('reference_audio');
  if (engine !== 'kokoro' && (!refInput.files || !refInput.files.length)) {
    toast('Upload a reference voice clip to clone for this engine'); return;
  }

  const fd = new FormData();
  for (const f of fileInput.files) { fd.append('files', f); }
  fd.append('voice',         document.getElementById('voice').value);
  fd.append('lang_code',     document.getElementById('lang').value);
  fd.append('speed',         document.getElementById('speed').value);
  fd.append('device',        document.getElementById('device').value);
  fd.append('trf',           document.getElementById('trf').checked);
  fd.append('merge',         document.getElementById('merge').checked);
  fd.append('chunk_size',    document.getElementById('chunk_size').value);
  fd.append('silence',       document.getElementById('silence').value);
  fd.append('min_ch_len',    document.getElementById('min_ch_len').value);
  fd.append('output_format',   document.querySelector('input[name="output_format"]:checked').value);
  fd.append('bitrate',         document.getElementById('bitrate').value);
  // A device-folder handle is a browser-side concept only — the container
  // has no use for its display name as a server path, so don't send one; the
  // server just uses its own default location, and saveFileToDevice() copies
  // each finished file into the chosen folder as it completes (see below).
  fd.append('custom_out_dir', _deviceDirHandle ? '' : document.getElementById('out_dir').value.trim());
  fd.append('chapter_indices', _getChapterIndices());
  fd.append('enhance',         document.getElementById('enhance').checked);
  fd.append('multi_voice',     document.getElementById('multi_voice').checked);
  fd.append('ambience',        document.getElementById('ambience').checked);
  fd.append('ollama_url',      document.getElementById('ollama_url').value.trim());
  fd.append('ollama_model',    document.getElementById('ollama_model').value);
  fd.append('engine',          engine);
  if (engine === 'kokoro') { fd.append('kokoro_workers', document.getElementById('kokoro_workers').value || '1'); }
  if (engine !== 'kokoro') { fd.append('reference_audio', refInput.files[0]); }
  if (engine === 'chatterbox') {
    fd.append('chatterbox_workers', document.getElementById('chatterbox_workers').value || '1');
    fd.append('chatterbox_speed',   document.getElementById('chatterbox_speed').value || '1.0');
    fd.append('chatterbox_cfg_weight',   document.getElementById('chatterbox_cfg_weight').value || '0.3');
    fd.append('chatterbox_exaggeration', document.getElementById('chatterbox_exaggeration').value || '0.7');
    fd.append('chatterbox_temperature',  document.getElementById('chatterbox_temperature').value || '0.8');
    fd.append('chatterbox_breaths',      document.getElementById('chatterbox_breaths').checked);
  }
  if (engine === 'higgs') {
    fd.append('higgs_temperature', document.getElementById('higgs_temperature').value || '0.15');
    fd.append('higgs_top_p',       document.getElementById('higgs_top_p').value || '0.75');
    fd.append('higgs_top_k',       document.getElementById('higgs_top_k').value || '25');
    fd.append('higgs_workers',     document.getElementById('higgs_workers').value || '1');
  }

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
    const r = await fetch('/api/convert', { method: 'POST', body: fd });
    if (!r.ok) throw new Error('Server error ' + r.status);
    const d = await r.json();

    batchJobIds = d.job_ids;

    if (d.reference_warnings && d.reference_warnings.length) {
      toast('Reference clip quality warning — see the log for details');
    }

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
  if (eventSource) eventSource.close();
  eventSource = new EventSource('/api/stream/' + id);
  eventSource.onmessage = e => { try { handleMsg(JSON.parse(e.data)); } catch(_) {} };
  eventSource.onerror   = () => { eventSource.close(); addLog('Connection lost.', 'warn'); };
}

function handleMsg(m) {
  if (m.type === 'log') {
    if (m.msg.includes('[Ollama]')) addOllamaLog(m.msg);
    else addLog(m.msg);
  }
  else if (m.type === 'status')   setStatus(m.msg);
  else if (m.type === 'progress') setProg(m.value, m.label);
  else if (m.type === 'file')     {
    addFile(m);
    if (m.chapter > 0) _chDone(m.chapter - 1, m.duration);
    if (_deviceDirHandle) {
      saveFileToDevice(jobId, m.filename).then(ok => { if (ok) toast('Saved "' + m.filename + '" to your device'); });
    }
  }
  else if (m.type === 'done')     onDone(m.files);
  else if (m.type === 'ch_info')  _initChGrid(m.chapters);
  else if (m.type === 'ch_start') _chStart(m.ch_i, m.chunks);
  else if (m.type === 'ch_prog')  _chProg(m.ch_i, m.pct);
  else if (m.type === 'ch_skip')  _chSkip(m.ch_i);
}

function onDone(files) {
  if (eventSource) eventSource.close();
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
    const src = new EventSource('/api/stream/' + id);
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
    if (_deviceDirHandle) saveFileToDevice(id, m.filename);
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
      a.href     = '/api/download/' + jobId + '/' + encodeURIComponent(fname);
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
    batchJobIds.forEach(id => fetch('/api/stop/' + id, { method: 'POST' }));
    Object.values(batchSources).forEach(s => s.close());
    batchSources = {};
  } else if (jobId) {
    fetch('/api/stop/' + jobId, { method: 'POST' });
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
  const card = document.getElementById('files-card');
  if (card) card.style.display = 'block';
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
    '<a class="btn-sm" href="/api/download/' + jobId + '/' +
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
  const outGrid   = document.getElementById('out-grid');
  const pathCard  = document.getElementById('out-path-card');
  const logEl     = document.getElementById('log');
  const flEl      = document.getElementById('file-list');
  const filesCard = document.getElementById('files-card');
  if (outGrid)   { outGrid.className = 'out-grid'; outGrid.innerHTML = ''; }
  if (pathCard)  pathCard.style.display = 'none';
  if (logEl)     logEl.innerHTML  = '';
  if (flEl)      flEl.innerHTML   = '';
  if (filesCard) filesCard.style.display = 'none';
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
    const resp = await fetch('/api/preview/' + voice);
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
    await fetch('/api/shutdown', { method: 'POST' });
  } catch(_) {}
  btn.innerHTML = '✓ Stopped';
  toast('Server stopped — you can close this tab.');
}

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3200);
}
