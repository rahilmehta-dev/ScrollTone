// ─── Voice Lab — batch Auto-tune tool page ─────────────────────────────────────
// Standalone page (frontend/voice-lab.html), self-contained script — does not
// share state with the main wizard's app.js. Uploads several reference clips
// at once, starts POST /api/autotune-batch-job (backend/routes/autotune.py,
// backend/voice_tuning.py run_batch_autotune_job), and streams results from
// the same job/SSE machinery every other job in this app uses
// (GET /api/stream/{job_id}). Two extra message types over the plain
// job/SSE baseline: "autotune_candidate_result" fires the moment each
// individual candidate is scored (live — the currently-running voice's card
// updates/reshuffles candidate by candidate), and "autotune_voice_result"
// fires once per completed voice with the final authoritative ranking.

const WORKERS_CAP = { chatterbox: 16, higgs: 4 };

let _labJobId = null;
let _labEventSource = null;
let _labVoices = [];          // [{index, filename}] — from ch_info, in order
let _labVoiceCardEls = {};    // voice_index -> card element
let _labLiveCandidates = {};  // voice_index -> [{params, score, filename}, ...] so far
let _labVoiceFilenames = {};  // voice_index -> filename, filled in from ch_info

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3200);
}

function onLabEngineChange() {
  const engine = document.getElementById('lab-engine').value;
  document.getElementById('lab-workers').max = WORKERS_CAP[engine];
  const workersInput = document.getElementById('lab-workers');
  if (Number(workersInput.value) > WORKERS_CAP[engine]) workersInput.value = WORKERS_CAP[engine];
}

function fmtBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}

function onVoiceFilesChange() {
  const input = document.getElementById('voice-files');
  const list  = document.getElementById('voice-file-list');
  list.innerHTML = '';
  if (!input.files || !input.files.length) return;
  for (const f of input.files) {
    const row = document.createElement('div');
    row.className = 'lab-file-row';
    row.textContent = f.name + '  (' + fmtBytes(f.size) + ')';
    list.appendChild(row);
  }
}

function _labLog(text, cls) {
  const card = document.getElementById('lab-log-card');
  const log  = document.getElementById('lab-log');
  card.style.display = 'block';
  const line = document.createElement('div');
  line.className = 'sl-line' + (cls ? ' ' + cls : '');
  const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  line.textContent = '[' + time + '] ' + text;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

function _setLabRunning(running) {
  document.getElementById('lab-run-btn').disabled = running;
  document.getElementById('lab-stop-btn').style.display = running ? 'inline-flex' : 'none';
  document.getElementById('lab-prog-track').style.display = running ? 'block' : 'none';
  if (running) {
    document.getElementById('lab-results-card').style.display = 'none';
    document.getElementById('lab-results').innerHTML = '';
    _labVoices = [];
    _labVoiceCardEls = {};
    _labLiveCandidates = {};
    _labVoiceFilenames = {};
  }
}

async function startBatchAutotune() {
  const filesInput = document.getElementById('voice-files');
  const files = filesInput.files ? Array.from(filesInput.files) : [];
  if (files.length < 1) { toast('Upload at least one voice clip'); return; }
  if (files.length > 8) { toast('Upload at most 8 voice clips at once'); return; }

  const engine = document.getElementById('lab-engine').value;
  const device = document.getElementById('lab-device').value;
  const candidates = document.getElementById('lab-candidates').value || '20';
  const seed = document.getElementById('lab-seed').value || '411';
  const workers = document.getElementById('lab-workers').value || '1';

  const fd = new FormData();
  fd.append('engine', engine);
  fd.append('device', device);
  fd.append('num_candidates', candidates);
  fd.append('seed', seed);
  if (engine === 'chatterbox') fd.append('chatterbox_workers', workers);
  else fd.append('higgs_workers', workers);
  for (const f of files) fd.append('reference_audio', f);

  _setLabRunning(true);
  document.getElementById('lab-status').textContent = 'Starting…';
  _labLog('Started — ' + engine + ' · device=' + device + ' · ' + files.length + ' voice(s) · ' + candidates + ' candidates each · seed=' + seed, 'sl-start');

  let jobId;
  try {
    const r = await fetch('/api/autotune-batch-job', { method: 'POST', body: fd });
    if (!r.ok) {
      let detail = '';
      try { detail = (await r.json()).detail || ''; } catch(_) {}
      throw new Error(detail || ('HTTP ' + r.status));
    }
    const d = await r.json();
    jobId = d.job_id;
  } catch (e) {
    document.getElementById('lab-status').textContent = 'Failed: ' + e.message;
    _labLog('Failed: ' + e.message, 'sl-err');
    _setLabRunning(false);
    return;
  }

  _labJobId = jobId;
  _connectLabStream(jobId, engine);
}

function stopBatchAutotune() {
  if (!_labJobId) return;
  const jobId = _labJobId;
  _labJobId = null;
  fetch('/api/stop/' + jobId, { method: 'POST' }).catch(() => {});
  if (_labEventSource) { _labEventSource.close(); _labEventSource = null; }
  document.getElementById('lab-status').textContent = 'Stopped.';
  _labLog('Stopped by user', 'sl-stop');
  _setLabRunning(false);
}

function _connectLabStream(jobId, engine) {
  const status = document.getElementById('lab-status');

  if (_labEventSource) _labEventSource.close();
  const es = new EventSource('/api/stream/' + jobId);
  _labEventSource = es;

  es.onmessage = e => {
    let m;
    try { m = JSON.parse(e.data); } catch(_) { return; }

    if (m.type === 'log') {
      const line = m.msg.trim();
      if (line) _labLog(line);
    } else if (m.type === 'status') {
      status.textContent = m.msg;
    } else if (m.type === 'ch_info') {
      _labVoices = m.chapters || [];
      _renderVoicePlaceholders();
    } else if (m.type === 'ch_start') {
      const card = _labVoiceCardEls[m.ch_i];
      if (card) card.querySelector('.voice-card-status').textContent = 'Synthesizing 0/' + m.chunks + ' candidates…';
    } else if (m.type === 'ch_prog') {
      const card = _labVoiceCardEls[m.ch_i];
      if (card) {
        const pct = Math.round((m.pct || 0) * 100);
        card.querySelector('.voice-card-status').textContent = 'Synthesizing — ' + pct + '%…';
      }
    } else if (m.type === 'ch_skip') {
      const card = _labVoiceCardEls[m.ch_i];
      if (card) {
        card.classList.remove('pending');
        card.classList.add('skipped');
        card.querySelector('.voice-card-status').innerHTML = '<span class="badge-skip">SKIPPED</span> — see log for the reason';
      }
    } else if (m.type === 'autotune_candidate_result') {
      const voiceIndex = m.ch_i;
      if (!_labLiveCandidates[voiceIndex]) _labLiveCandidates[voiceIndex] = [];
      _labLiveCandidates[voiceIndex].push({ params: m.params, score: m.score, filename: m.filename });
      _renderVoiceCandidates(
        jobId, voiceIndex, _labVoiceFilenames[voiceIndex],
        _labLiveCandidates[voiceIndex], m.best_score, /*live=*/true,
      );
    } else if (m.type === 'autotune_voice_result') {
      _renderVoiceCandidates(
        jobId, m.voice_index, m.voice_filename, m.candidates, m.winner.score, /*live=*/false,
      );
    } else if (m.type === 'done') {
      if (_labEventSource) { _labEventSource.close(); _labEventSource = null; }
      _labJobId = null;
      status.textContent = 'Done.';
      _labLog('Done', 'sl-done');
      _setLabRunning(false);
    }
  };

  es.onerror = () => {
    if (_labJobId === jobId) {
      _labJobId = null;
      status.textContent = 'Failed: connection lost.';
      _labLog('Failed: connection lost', 'sl-err');
    }
    if (_labEventSource) { _labEventSource.close(); _labEventSource = null; }
    _setLabRunning(false);
  };
}

function _renderVoicePlaceholders() {
  const box = document.getElementById('lab-results');
  box.innerHTML = '';
  document.getElementById('lab-results-card').style.display = 'block';
  _labVoiceCardEls = {};
  _labVoiceFilenames = {};

  _labVoices.forEach(v => {
    const card = document.createElement('div');
    card.className = 'voice-card pending';
    card.innerHTML =
      '<div class="voice-card-hdr">' +
        '<span class="voice-card-title">' + escapeHtml(v.title) + '</span>' +
      '</div>' +
      '<div class="voice-card-status" style="font-size:.78rem;color:var(--txt-s)">Waiting…</div>';
    box.appendChild(card);
    _labVoiceCardEls[v.i] = card;
    _labVoiceFilenames[v.i] = v.title;
  });
}

// Renders one voice's card from whatever candidates have scored so far —
// called incrementally (live=true, as each autotune_candidate_result
// streams in for the CURRENTLY running voice — batch mode processes voices
// one at a time, so only one card is ever "live" at once) and once
// authoritatively at the end of that voice (live=false, from
// autotune_voice_result). Sorts every call so the DOM always reflects
// current ranking regardless of arrival order.
function _renderVoiceCandidates(jobId, voiceIndex, voiceFilename, candidates, bestScore, live) {
  const card = _labVoiceCardEls[voiceIndex];
  if (!card) return;
  card.classList.remove('pending');

  const sorted = [...candidates].sort((a, b) => b.score - a.score);
  const rows = sorted.map((c, idx) => {
    const isBest = c.score === bestScore;
    const paramStr = Object.entries(c.params).map(([k, v]) =>
      k + '=' + (typeof v === 'number' ? (Number.isInteger(v) ? v : v.toFixed(2)) : v)).join(' · ');
    return (
      '<div class="candidate-row' + (isBest ? ' best' : '') + '">' +
        '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">' +
          '<span style="font-weight:700">#' + (idx + 1) + '</span>' +
          (isBest ? '<span class="badge-best">BEST</span>' : '') +
          '<span style="font-size:.8rem;color:var(--txt-m)">score ' + c.score.toFixed(2) + '</span>' +
          '<span style="font-size:.72rem;color:var(--txt-s);font-family:monospace">' + paramStr + '</span>' +
        '</div>' +
        '<audio controls style="width:100%;height:30px;margin-top:6px" src="/api/download/' + jobId + '/' + encodeURIComponent(c.filename) + '"></audio>' +
      '</div>'
    );
  }).join('');

  const badge = live
    ? '<span style="font-size:.65rem;font-weight:800;letter-spacing:.06em;color:var(--txt-s);background:rgba(255,255,255,.06);padding:2px 6px;border-radius:4px">' + sorted.length + ' scored so far · best ' + bestScore.toFixed(2) + '</span>'
    : '<span style="font-size:.65rem;font-weight:800;letter-spacing:.06em;color:var(--success);background:rgba(16,185,129,.12);padding:2px 6px;border-radius:4px">DONE · best score ' + bestScore.toFixed(2) + '</span>';

  card.innerHTML =
    '<div class="voice-card-hdr">' +
      '<span class="voice-card-title">' + escapeHtml(voiceFilename) + '</span>' +
      badge +
    '</div>' +
    rows;
}

function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s;
  return div.innerHTML;
}
