/* dod-ic-recruiter dashboard.js — pure vanilla JS, no libraries */

let skillTags = [];
let runBtnDisabled = false;
let lastThreshold = 70;
let lastModified = 0;
let pollInterval = null;

/* ------------------------------------------------------------------ */
/* TAG INPUT                                                            */
/* ------------------------------------------------------------------ */

function addTag(s) {
  if (!s || skillTags.includes(s)) return;
  skillTags.push(s);
  renderTags();
}

function renderTags() {
  const list = document.getElementById('tags-list');
  list.innerHTML = '';
  skillTags.forEach((tag, idx) => {
    const span = document.createElement('span');
    span.className = 'tag';
    span.appendChild(document.createTextNode(tag));

    const btn = document.createElement('button');
    btn.className = 'tag-remove';
    btn.innerHTML = '&times;';
    btn.setAttribute('aria-label', 'Remove ' + tag);
    btn.onclick = () => {
      skillTags.splice(idx, 1);
      renderTags();
    };

    span.appendChild(btn);
    list.appendChild(span);
  });
}

/* ------------------------------------------------------------------ */
/* TAB SWITCHING                                                        */
/* ------------------------------------------------------------------ */

function switchToTab(name) {
  const btn = name === 'results'
    ? document.getElementById('tab-results')
    : document.getElementById('tab-input');
  if (btn) btn.click();
}

/* ------------------------------------------------------------------ */
/* STATUS BAR                                                           */
/* ------------------------------------------------------------------ */

function updateStatusBar(msg) {
  const bar = document.getElementById('status-bar');
  if (bar) bar.textContent = msg;
}

/* ------------------------------------------------------------------ */
/* RUN BUTTON RESET                                                     */
/* ------------------------------------------------------------------ */

function resetRunBtn() {
  const btn = document.getElementById('run-btn');
  btn.disabled = false;
  btn.textContent = 'Run Search';
  runBtnDisabled = false;
}

/* ------------------------------------------------------------------ */
/* AUTO-POLLING                                                         */
/* ------------------------------------------------------------------ */

function startPolling() {
  if (pollInterval) return;
  pollInterval = setInterval(checkForResults, 15000);
}

function checkForResults() {
  fetch('http://localhost:5000/results-modified')
    .then(r => r.json())
    .then(d => {
      if (d.modified > lastModified) {
        lastModified = d.modified;
        loadResults();
      }
    })
    .catch(() => {});
}

function loadResults() {
  fetch('http://localhost:5000/results')
    .then(r => r.json())
    .then(d => {
      if (d.status === 'no_results') {
        updateStatusBar('No results yet — run a search first.');
        return;
      }
      renderResults(d.data);
      switchToTab('results');
      if (runBtnDisabled) {
        resetRunBtn();
      }
    })
    .catch(() => {});
}

/* ------------------------------------------------------------------ */
/* RENDER RESULTS                                                       */
/* ------------------------------------------------------------------ */

function renderResults(data) {
  updateStatusBar('Results loaded — ' + data.role_title + ' — ' + data.run_at);

  /* metadata grid */
  const grid = document.getElementById('metadata-grid');
  if (grid) {
    grid.style.display = '';
    grid.innerHTML = '';

    const fields = [
      ['Role', data.role_title],
      ['Timestamp', data.run_at],
      ['Candidates Evaluated', data.candidates_evaluated],
      ['Sources', Array.isArray(data.sources) ? data.sources.join(', ') : data.sources],
      ['Dedup Summary', data.dedup_summary],
      ['Threshold Used', (data.match_threshold_used || lastThreshold || 70) + '%'],
    ];

    fields.forEach(([label, val]) => {
      const dt = document.createElement('dt');
      dt.textContent = label;
      const dd = document.createElement('dd');
      dd.textContent = val != null ? val : '—';
      grid.appendChild(dt);
      grid.appendChild(dd);
    });
  }

  /* table */
  const tbody = document.getElementById('results-body');
  const table = document.getElementById('results-table');
  if (!tbody || !table) return;

  tbody.innerHTML = '';
  table.style.display = '';

  const threshold = (data.match_threshold_used || lastThreshold || 70) / 100;

  (data.candidates || []).forEach(candidate => {
    const score = candidate.overall_score;

    let rowClass;
    if (score < 0.50) {
      rowClass = 'weak-match';
    } else if (score < threshold) {
      rowClass = 'below-threshold';
    } else if (score >= 0.75) {
      rowClass = 'strong-match';
    } else {
      rowClass = 'ok-match';
    }

    /* main row */
    const tr = document.createElement('tr');
    tr.className = rowClass;
    tr.dataset.candidateId = candidate.candidate_id;
    tr.dataset.rank = candidate.rank;

    const sources = Array.isArray(candidate.sources) ? candidate.sources.join(', ') : (candidate.sources || '');
    const topSkills = Array.isArray(candidate.top_skills)
      ? candidate.top_skills.slice(0, 3).join(', ')
      : '';
    const flags = Array.isArray(candidate.flags) ? candidate.flags : [];
    let flagsText = flags.join(' · ');
    const belowBadge = rowClass === 'below-threshold'
      ? ' <span class="badge badge-below">Below Threshold</span>'
      : '';

    tr.innerHTML = [
      '<td>' + (candidate.rank || '') + '</td>',
      '<td>' + escHtml(candidate.name || '') + '</td>',
      '<td>' + escHtml(sources) + '</td>',
      '<td>' + Math.round(score * 100) + '%</td>',
      '<td>' + escHtml(candidate.clearance_inference_level || '') + '</td>',
      '<td>' + escHtml(topSkills) + '</td>',
      '<td>' + escHtml(flagsText) + belowBadge + '</td>',
    ].join('');

    /* detail row */
    const detailTr = document.createElement('tr');
    detailTr.className = 'detail-row';
    detailTr.hidden = true;

    const detailTd = document.createElement('td');
    detailTd.colSpan = 7;

    /* inferred skills html */
    const inferredSkills = Array.isArray(candidate.inferred_skills) ? candidate.inferred_skills : [];
    let inferredHtml = '';
    if (inferredSkills.length === 0) {
      inferredHtml = '<em>None inferred</em>';
    } else {
      inferredHtml = '<ul class="inferred-list">';
      inferredSkills.forEach(s => {
        const conf = s.confidence || 0;
        let badgeClass = 'badge-low';
        if (conf >= 0.75) badgeClass = 'badge-high';
        else if (conf >= 0.5) badgeClass = 'badge-med';
        inferredHtml += '<li>'
          + escHtml(s.skill || '')
          + ' <span class="badge ' + badgeClass + '">' + Math.round(conf * 100) + '%</span>'
          + ' — <em>' + escHtml(s.source || '') + '</em>: '
          + escHtml(s.justification || '')
          + '</li>';
      });
      inferredHtml += '</ul>';
    }

    const explicitSkills = Array.isArray(candidate.explicit_skills) && candidate.explicit_skills.length
      ? candidate.explicit_skills.join(', ')
      : 'None listed';

    const skillGaps = Array.isArray(candidate.skill_gaps) && candidate.skill_gaps.length
      ? candidate.skill_gaps.join(', ')
      : 'None identified';

    const flagsList = flags.length
      ? '<ul>' + flags.map(f => '<li>' + escHtml(f) + '</li>').join('') + '</ul>'
      : '<em>None</em>';

    detailTd.innerHTML = [
      '<div class="detail-inner">',
      '<p class="reasoning">' + escHtml(candidate.reasoning || '') + '</p>',
      '<p><strong>Explicit Skills:</strong> ' + escHtml(explicitSkills) + '</p>',
      '<div><strong>Inferred Skills:</strong>' + inferredHtml + '</div>',
      '<p><strong>Skill Gaps:</strong> ' + escHtml(skillGaps) + '</p>',
      '<div><strong>Flags:</strong>' + flagsList + '</div>',
      '<div class="note-area">',
      '  <textarea class="note-input" rows="3" placeholder="Add a note about this candidate..."></textarea>',
      '  <button class="save-note-btn">Save Note</button>',
      '  <span class="note-status"></span>',
      '</div>',
      '</div>',
    ].join('');

    /* save note handler */
    const saveBtn = detailTd.querySelector('.save-note-btn');
    const noteInput = detailTd.querySelector('.note-input');
    const noteStatus = detailTd.querySelector('.note-status');

    saveBtn.onclick = () => {
      const noteVal = noteInput.value;
      noteStatus.textContent = 'Saving...';
      fetch('http://localhost:5000/save-note', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ candidate_id: candidate.candidate_id, note: noteVal }),
      })
        .then(r => {
          if (!r.ok) throw new Error('Server error ' + r.status);
          return r.json();
        })
        .then(() => { noteStatus.textContent = 'Saved.'; })
        .catch(e => { noteStatus.textContent = 'Error: ' + e.message; });
    };

    detailTr.appendChild(detailTd);

    /* toggle detail on main row click */
    tr.style.cursor = 'pointer';
    tr.onclick = () => {
      detailTr.hidden = !detailTr.hidden;
    };

    tbody.appendChild(tr);
    tbody.appendChild(detailTr);
  });

  table.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/* ------------------------------------------------------------------ */
/* HTML ESCAPE HELPER                                                   */
/* ------------------------------------------------------------------ */

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/* ------------------------------------------------------------------ */
/* DOM READY                                                            */
/* ------------------------------------------------------------------ */

document.addEventListener('DOMContentLoaded', () => {

  /* --- Tab switching --- */
  const tabInput = document.getElementById('tab-input');
  const tabResults = document.getElementById('tab-results');
  const sectionInput = document.getElementById('section-input');
  const sectionResults = document.getElementById('section-results');

  function activateTab(name) {
    if (name === 'input') {
      tabInput.classList.add('active');
      tabResults.classList.remove('active');
      sectionInput.classList.add('active');
      sectionResults.classList.remove('active');
    } else {
      tabResults.classList.add('active');
      tabInput.classList.remove('active');
      sectionResults.classList.add('active');
      sectionInput.classList.remove('active');
    }
  }

  tabInput.addEventListener('click', () => activateTab('input'));
  tabResults.addEventListener('click', () => activateTab('results'));

  /* default: input active */
  activateTab('input');

  /* --- Tag input --- */
  const tagInput = document.getElementById('tag-input');
  if (tagInput) {
    tagInput.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ',') {
        e.preventDefault();
        const val = tagInput.value.replace(/,/g, '').trim();
        addTag(val);
        tagInput.value = '';
      }
    });
  }

  /* --- Threshold slider --- */
  const thresholdEl = document.getElementById('threshold');
  const thresholdDisplay = document.getElementById('threshold-display');
  if (thresholdEl && thresholdDisplay) {
    thresholdDisplay.textContent = thresholdEl.value + '%';
    thresholdEl.addEventListener('input', () => {
      thresholdDisplay.textContent = thresholdEl.value + '%';
    });
  }

  /* --- File input --- */
  const icimsFile = document.getElementById('icims-file');
  const fileInfo = document.getElementById('file-info');
  if (icimsFile && fileInfo) {
    icimsFile.addEventListener('change', () => {
      if (icimsFile.files && icimsFile.files.length > 0) {
        const f = icimsFile.files[0];
        const sizeKB = f.size / 1024;
        const sizeStr = sizeKB >= 1024
          ? (sizeKB / 1024).toFixed(2) + ' MB'
          : sizeKB.toFixed(1) + ' KB';
        fileInfo.textContent = f.name + ' (' + sizeStr + ')';
        fileInfo.style.display = '';
      } else {
        fileInfo.style.display = 'none';
        fileInfo.textContent = '';
      }
    });
  }

  /* --- Run Search --- */
  const runBtn = document.getElementById('run-btn');
  const jdTextarea = document.getElementById('job-description');
  const jdError = document.getElementById('jd-error');
  const uploadStatus = document.getElementById('upload-status');
  const runStatus = document.getElementById('run-status');

  runBtn.addEventListener('click', async () => {
    if (runBtnDisabled) return;

    /* 1. Validate JD */
    const jdVal = jdTextarea ? jdTextarea.value.trim() : '';
    if (!jdVal) {
      if (jdError) {
        jdError.style.display = '';
        jdError.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
      return;
    }
    if (jdError) jdError.style.display = 'none';

    /* 2. Upload iCIMS file if provided */
    let icimsUploaded = false;
    let icimsFilename = '';

    if (icimsFile && icimsFile.files && icimsFile.files.length > 0) {
      const file = icimsFile.files[0];
      icimsFilename = file.name;

      if (uploadStatus) uploadStatus.textContent = 'Uploading...';

      const formData = new FormData();
      formData.append('file', file);

      let uploadOk = false;
      try {
        const resp = await fetch('http://localhost:5000/upload-icims', {
          method: 'POST',
          body: formData,
        });
        if (!resp.ok) {
          const errText = await resp.text();
          if (uploadStatus) uploadStatus.textContent = 'Upload error: ' + errText;
          return;
        }
        uploadOk = true;
      } catch (e) {
        if (uploadStatus) uploadStatus.textContent = 'Upload error: ' + e.message;
        return;
      }

      if (uploadOk) {
        icimsUploaded = true;
        if (uploadStatus) uploadStatus.textContent = 'Saved to imports/ — will be included automatically.';
      }
    }

    /* 3. Build payload */
    const thresholdVal = thresholdEl ? parseInt(thresholdEl.value, 10) : 70;
    const payload = {
      written_at: new Date().toISOString(),
      job_description: jdTextarea ? jdTextarea.value : '',
      required_skills_override: skillTags.slice(),
      match_threshold: thresholdVal,
      icims_file_uploaded: icimsUploaded,
      icims_filename: icimsFilename,
    };

    /* 4. POST /save-search */
    try {
      const saveResp = await fetch('http://localhost:5000/save-search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!saveResp.ok) {
        const errText = await saveResp.text();
        if (runStatus) runStatus.textContent = 'Error saving search: ' + errText;
        return;
      }
    } catch (e) {
      if (runStatus) runStatus.textContent = 'Error saving search: ' + e.message;
      return;
    }

    /* 5. Disable run button, update status */
    runBtn.disabled = true;
    runBtn.textContent = 'Waiting for Claude Code...';
    runBtnDisabled = true;
    lastThreshold = payload.match_threshold;

    if (runStatus) {
      runStatus.textContent = 'Search inputs saved. Switch to Claude Code and invoke the dod-ic-recruiter skill to begin. This page will automatically switch to Results when Claude Code finishes.';
    }

    /* 6. Ensure polling is running */
    startPolling();
  });

  /* --- Initial load --- */
  startPolling();
  loadResults();
});
