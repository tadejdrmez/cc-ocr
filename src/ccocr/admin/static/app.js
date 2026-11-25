// Small progressive enhancements; every page also works without JavaScript.
(function () {
  function csrf() { var i = document.querySelector('input[name=csrf]'); return i ? i.value : ''; }
  function post(url, data) {
    var fd = new FormData();
    Object.keys(data).forEach(function (k) { if (data[k] !== false && data[k] != null) fd.append(k, data[k]); });
    fd.append('csrf', csrf());
    return fetch(url, { method: 'POST', body: fd, credentials: 'same-origin' }).then(function (r) { return r.json(); });
  }
  // Rule tester
  document.querySelectorAll('[data-rule-test]').forEach(function (box) {
    var btn = box.querySelector('button'), out = box.querySelector('.out');
    btn.addEventListener('click', function () {
      var form = btn.closest('form') || document;
      var expr = (form.querySelector('[name=expr]') || {}).value || '';
      post('/rules/test', {
        expr: expr, text: box.querySelector('[name=sample]').value, zone: box.dataset.zone,
        page: box.querySelector('[name=page]').value, prev_blank: box.querySelector('[name=prev_blank]').checked ? '1' : false
      }).then(function (r) {
        out.className = 'out banner ' + (!r.ok ? 'bad' : r.match ? 'ok' : 'warn');
        out.textContent = !r.ok ? 'Expression error: ' + r.error : r.match ? 'Match: a split would be proposed before this page.' : 'No match.';
      });
    });
  });
  // Naming preview
  document.querySelectorAll('[data-naming]').forEach(function (form) {
    var out = form.querySelector('.preview');
    function run() {
      post('/naming/preview', {
        pattern: form.querySelector('[name="output.naming"]').value,
        max_length: form.querySelector('[name="output.max_length"]').value,
        date_format: form.querySelector('[name="output.date_format"]').value
      }).then(function (r) {
        out.className = 'preview banner ' + (r.ok ? 'ok' : 'bad');
        out.textContent = r.ok ? r.name + '  (' + r.length + ' characters)' : r.error;
      });
    }
    form.querySelectorAll('input,select').forEach(function (el) { el.addEventListener('input', run); });
    run();
  });
  // Folder test
  document.querySelectorAll('[data-folder-test]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var out = document.getElementById('folder-test-out');
      out.textContent = 'Testing…'; out.className = 'banner acc';
      post(btn.dataset.folderTest, {}).then(function (r) {
        out.className = 'banner ' + (r.ok ? 'ok' : 'bad');
        out.innerHTML = '';
        r.steps.forEach(function (s) { var d = document.createElement('div'); d.textContent = (s.ok ? '✓ ' : '✗ ') + s.text; out.appendChild(d); });
      });
    });
  });
  // Reason box appears when a field that needs a justification changes
  document.querySelectorAll('[data-reason-for]').forEach(function (box) {
    var input = document.querySelector('[name="' + box.dataset.reasonFor + '"]');
    function upd() { box.hidden = input.checked === (input.dataset.initial === '1'); }
    input.addEventListener('change', upd); upd();
  });
  // Confirm buttons (no browser dialogs): first click arms, second click submits
  document.querySelectorAll('[data-confirm]').forEach(function (btn) {
    btn.addEventListener('click', function (e) {
      if (btn.dataset.armed) return;
      e.preventDefault(); btn.dataset.armed = '1'; var t = btn.textContent; btn.textContent = btn.dataset.confirm;
      setTimeout(function () { delete btn.dataset.armed; btn.textContent = t; }, 4000);
    });
  });
})();
// Separation screen: documents = kept pages in batch order, cut where a document starts.
(function () {
  var root = document.getElementById('sep'), dataEl = document.getElementById('plan-data');
  if (!root || !dataEl) return;
  var job = root.dataset.job, data = JSON.parse(dataEl.textContent), pages = data.pages, n = pages.length;
  var cut = {}, removed = {}, rot = {}, meta = {}, sel = 0, proposal = {}, initial;
  data.proposals.forEach(function (p) { proposal[p.before_page] = p; });
  var kept = {};
  data.documents.forEach(function (d, i) {
    if (i > 0) cut[d.pages[0]] = true;
    d.pages.forEach(function (p) { kept[p] = true; });
    meta[d.pages[0]] = { doctype: d.doctype || '', classification: d.classification || '', flags: d.review_flags || [], by: d.started_by || '' };
  });
  for (var i = 0; i < n; i++) if (!kept[i]) removed[i] = true;
  // A cut in the plan sits before the first kept page of a document; also mark it
  // right after removed pages so moving between them feels natural.
  function documents() {
    var docs = [], cur = null, pending = false;
    for (var i = 0; i < n; i++) {
      if (cut[i]) pending = true;
      if (removed[i]) continue;
      if (!cur || pending) { cur = { pages: [] }; docs.push(cur); pending = false; }
      cur.pages.push(i);
    }
    docs.forEach(function (d) { var m = meta[d.pages[0]] || {}; d.doctype = m.doctype || ''; d.classification = m.classification || ''; d.flags = m.flags || []; d.by = m.by || ''; });
    return docs;
  }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function thumb(i, large) { return '/review/' + encodeURIComponent(job) + '/page/' + (i + 1) + '.jpg' + (large ? '?size=l' : ''); }
  function pageCard(i) {
    var p = pages[i], b = el('button', 'pg' + (removed[i] ? ' rm' : '') + (i === sel ? ' sel' : ''));
    b.type = 'button'; b.setAttribute('aria-label', 'Page ' + (i + 1));
    var im = el('div', 'im'), img = el('img'); img.src = thumb(i); img.alt = ''; img.loading = 'lazy';
    img.style.transform = rot[i] ? 'rotate(' + rot[i] + 'deg)' : ''; im.appendChild(img); b.appendChild(im);
    var nn = el('div', 'n'); nn.appendChild(el('span', '', String(i + 1)));
    if (p.separator) nn.appendChild(el('i', 's', 'sheet' + (p.separator.doctype ? ' ' + p.separator.doctype : '')));
    if (p.blank) nn.appendChild(el('i', '', 'blank'));
    if (p.flags && p.flags.length) nn.appendChild(el('i', 'b', '!'));
    if (rot[i]) nn.appendChild(el('i', 'w', '↻' + rot[i]));
    if (removed[i]) nn.appendChild(el('i', 'b', 'left out'));
    b.appendChild(nn);
    b.addEventListener('click', function () { sel = i; draw(); });
    return b;
  }
  function cutButton(i, on) {
    var c = el('button', 'cut' + (proposal[i] ? ' prop' : '') + (on ? ' on' : ''));
    c.type = 'button';
    var why = proposal[i] ? ' (proposed: ' + proposal[i].reason + ')' : '';
    c.title = (on ? 'Join with the previous document' : 'Start a new document at page ' + (i + 1)) + why;
    c.setAttribute('aria-label', c.title);
    c.addEventListener('click', function () {
      if (on) { for (var k = i; k > 0 && (k === i || removed[k]); k--) delete cut[k]; } else { cut[i] = true; }
      draw();
    });
    return c;
  }
  function select(opts, value, onchange, label) {
    var s = el('select'); s.setAttribute('aria-label', label);
    var o = el('option', '', '(as proposed)'); o.value = ''; s.appendChild(o);
    opts.forEach(function (x) { var o = el('option', '', x[1]); o.value = x[0]; if (x[0] === value) o.selected = true; s.appendChild(o); });
    if (value && !opts.some(function (x) { return x[0] === value; })) { var o2 = el('option', '', value); o2.value = value; o2.selected = true; s.appendChild(o2); }
    s.addEventListener('change', function () { onchange(s.value); }); return s;
  }
  function draw() {
    var strip = document.getElementById('strip'), docs = documents(); strip.innerHTML = '';
    var byFirst = {}; docs.forEach(function (d, k) { byFirst[d.pages[0]] = k; });
    var box = null, wrap = null, docNo = 0;
    function openDoc(first) {
      var d = docs[byFirst[first]]; docNo++;
      box = el('div', 'doc'); var h = el('div', 'dh');
      h.appendChild(el('b', '', 'Document ' + docNo));
      h.appendChild(el('span', 'muted', d.pages.length + (d.pages.length === 1 ? ' page' : ' pages')));
      h.appendChild(select(data.doctypes, d.doctype, function (v) { (meta[first] = meta[first] || {}).doctype = v; summary(); }, 'Document type'));
      var c = el('input'); c.setAttribute('list', 'cls-list'); c.placeholder = 'classification'; c.value = d.classification; c.setAttribute('aria-label', 'Classification');
      c.addEventListener('input', function () { (meta[first] = meta[first] || {}).classification = c.value; summary(); }); h.appendChild(c);
      if (d.by) h.appendChild(el('span', 'muted small', d.by));
      d.flags.forEach(function (f) { h.appendChild(el('span', 'pill warn', f)); });
      box.appendChild(h); wrap = el('div', 'pages'); box.appendChild(wrap); strip.appendChild(box);
    }
    for (var i = 0; i < n; i++) {
      if (!removed[i] && byFirst[i] !== undefined) {
        if (box && i > 0) wrap.appendChild(cutButton(i, true));
        openDoc(i);
      } else if (i > 0 && wrap) {
        wrap.appendChild(cutButton(i, false));
      }
      if (!wrap) { box = el('div', 'doc removed'); box.appendChild(el('div', 'dh', 'Removed before the first document')); wrap = el('div', 'pages'); box.appendChild(wrap); strip.appendChild(box); }
      wrap.appendChild(pageCard(i));
    }
    preview(); summary();
  }
  function preview() {
    var p = pages[sel];
    document.getElementById('pv-title').textContent = 'Page ' + (sel + 1) + ' of ' + n;
    var img = document.getElementById('pv-img'), side = rot[sel] === 90 || rot[sel] === 270;
    img.src = thumb(sel, true); img.style.transform = rot[sel] ? 'rotate(' + rot[sel] + 'deg)' : '';
    img.style.maxHeight = side ? img.parentNode.clientWidth + 'px' : ''; img.style.maxWidth = side ? '60vh' : '';
    var rm = document.getElementById('pv-rm'); rm.textContent = removed[sel] ? 'Keep page' : 'Remove page';
    var info = document.getElementById('pv-info'); info.innerHTML = '';
    var facts = [];
    if (p.separator) facts.push('Separator sheet' + (p.separator.valid ? (p.separator.doctype ? ': ' + p.separator.doctype : '') : ' (unreadable)'));
    if (p.blank) facts.push('Blank page (ink ' + (p.ink * 100).toFixed(2) + ' %)');
    if (p.words) facts.push(p.words + ' words, confidence ' + p.confidence + ', ' + p.languages);
    if (proposal[sel]) facts.push('Split proposed before this page: ' + proposal[sel].reason);
    (p.notes || []).forEach(function (x) { facts.push(x); });
    (p.flags || []).forEach(function (x) { facts.push('⚠ ' + x); });
    facts.forEach(function (f) { info.appendChild(el('div', '', f)); });
    document.getElementById('pv-text').textContent = p.text || '(no text)';
  }
  function summary() {
    var docs = documents(), rmList = [], rotList = [];
    for (var i = 0; i < n; i++) { if (removed[i]) rmList.push(i + 1); if (rot[i]) rotList.push((i + 1) + ' (' + rot[i] + '°)'); }
    document.getElementById('sum').textContent = docs.length + ' document' + (docs.length === 1 ? '' : 's');
    var ch = document.getElementById('changes'); ch.innerHTML = '';
    var ul = el('ul');
    ul.appendChild(el('li', '', docs.length + ' document(s): ' + docs.map(function (d) { return pages_(d.pages); }).join(' · ')));
    ul.appendChild(el('li', '', rmList.length ? 'Pages left out: ' + rmList.join(', ') : 'No pages left out'));
    if (rotList.length) ul.appendChild(el('li', '', 'Turned and read again: ' + rotList.join(', ')));
    if (initial && JSON.stringify(layout().documents.map(function (d) { return d.pages; })) !== initial) ul.appendChild(el('li', '', 'Changed from the proposal'));
    ch.appendChild(ul);
    document.getElementById('layout').value = JSON.stringify(layout());
  }
  function pages_(list) {
    var out = [], s = null, prev = null;
    list.concat([null]).forEach(function (x) {
      if (x !== null && prev !== null && x === prev + 1) { prev = x; return; }
      if (s !== null) out.push(s === prev ? String(s + 1) : (s + 1) + '–' + (prev + 1));
      s = prev = x;
    });
    return out.join(',');
  }
  function layout() {
    return { documents: documents().map(function (d) { return { pages: d.pages, doctype: d.doctype, classification: d.classification }; }), rotate: rot };
  }
  document.getElementById('pv-rot').addEventListener('click', function () { rot[sel] = ((rot[sel] || 0) + 90) % 360; if (!rot[sel]) delete rot[sel]; draw(); });
  document.getElementById('pv-rm').addEventListener('click', function () { if (removed[sel]) delete removed[sel]; else removed[sel] = true; draw(); });
  document.addEventListener('keydown', function (e) {
    if (/INPUT|SELECT|TEXTAREA/.test((e.target || {}).tagName)) return;
    if (e.key === 'ArrowRight' && sel < n - 1) { sel++; draw(); } else if (e.key === 'ArrowLeft' && sel > 0) { sel--; draw(); }
    else if (e.key === 'r') document.getElementById('pv-rot').click();
    else if (e.key === 'x' || e.key === 'Delete') document.getElementById('pv-rm').click();
  });
  draw();
  initial = JSON.stringify(layout().documents.map(function (d) { return d.pages; }));
  summary();
})();
