(function () {
  // Attach the token to every htmx request.
  document.body.addEventListener('htmx:configRequest', function (e) {
    var t = localStorage.getItem('omnifetch_token');
    if (t) e.detail.headers['X-OmniFetch-Token'] = t;
  });

  // Server signals toasts via the HX-Trigger header.
  document.body.addEventListener('toast', function (e) {
    showToast(typeof e.detail === 'string' ? e.detail : JSON.stringify(e.detail));
  });
  window.showToast = showToast;
  function showToast(msg) {
    var box = document.getElementById('toast-box');
    var el = document.createElement('div');
    el.className = 'toast';
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(function () { el.remove(); }, 3500);
  }

  function openTokenModal() {
    var m = document.getElementById('token-modal');
    if (m) m.style.display = 'flex';
  }

  document.addEventListener('DOMContentLoaded', function () {
    // Auth probe: show the token modal when the server requires one.
    fetch('/api/health').then(function (r) { return r.json(); }).then(function (j) {
      if (j.auth_required && !localStorage.getItem('omnifetch_token')) openTokenModal();
    }).catch(function () { /* offline: stay quiet */ });

    // Any authenticated fragment that 401s -> ask for the token again.
    document.body.addEventListener('htmx:responseError', function (e) {
      if (e.detail.xhr && e.detail.xhr.status === 401) {
        // The UI login guard sends us to /login instead.
        if (e.detail.xhr.getResponseHeader('HX-Redirect')) return;
        localStorage.removeItem('omnifetch_token');
        document.cookie = 'omnifetch_token=; path=/; max-age=0; samesite=lax';
        openTokenModal();
      }
    });

    var form = document.getElementById('token-form');
    if (form) form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var input = document.getElementById('token-input');
      var err = document.getElementById('token-error');
      var token = input.value.trim();
      fetch('/api/auth/verify', { headers: { 'X-OmniFetch-Token': token } }).then(function (r) {
        if (r.ok) {
          localStorage.setItem('omnifetch_token', token);
          document.cookie = 'omnifetch_token=' + encodeURIComponent(token) +
            '; path=/; samesite=lax';
          location.reload();
        } else {
          err.textContent = 'Token 不正确';
        }
      });
    });
  });
})();
