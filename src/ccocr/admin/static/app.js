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
