/* Fireside i18n runtime.
 *
 * The host negotiates a language for every page (query, cookie, then
 * Accept-Language), injects that catalog as window.__FS_I18N__, and sets
 * <html lang> + dir. Nothing is fetched after load, so there is no flash of
 * the wrong language and no ordering problem with the app script.
 *
 * Keys are the English sentence itself (gettext style). A key with no
 * translation falls back to the key, so a half-translated catalog degrades to
 * English instead of showing raw identifiers.
 *
 *   static markup:  <button data-t>Sign in</button>
 *                   <input data-t-ph placeholder="Username">
 *   script:         T('Sign in')
 *                   T('Chapter {n}', {n: 3})
 */
(function () {
  var C = window.__FS_I18N__ || { locale: 'en', dir: 'ltr', messages: {}, locales: [] };

  function fmt(s, vars) {
    if (!vars) return s;
    return s.replace(/\{(\w+)\}/g, function (m, k) {
      return Object.prototype.hasOwnProperty.call(vars, k) ? String(vars[k]) : m;
    });
  }

  function t(msgid, vars) {
    if (typeof msgid !== 'string') return msgid;
    var s = C.messages[msgid];
    return fmt(s === undefined ? msgid : s, vars);
  }

  function apply(root) {
    root = root || document;
    var q = function (sel) { return Array.prototype.slice.call(root.querySelectorAll(sel)); };
    q('[data-t]').forEach(function (el) {
      var key = el.getAttribute('data-t') || el.textContent.trim();
      if (!el.hasAttribute('data-t')) el.setAttribute('data-t', key);
      el.textContent = t(key);
    });
    q('[data-t-ph]').forEach(function (el) {
      var key = el.getAttribute('data-t-ph') || el.getAttribute('placeholder') || '';
      if (!el.hasAttribute('data-t-ph')) el.setAttribute('data-t-ph', key);
      el.setAttribute('placeholder', t(key));
    });
    q('[data-t-title]').forEach(function (el) {
      var key = el.getAttribute('data-t-title') || el.getAttribute('title') || '';
      if (!el.hasAttribute('data-t-title')) el.setAttribute('data-t-title', key);
      el.setAttribute('title', t(key));
    });
    q('[data-t-aria]').forEach(function (el) {
      var key = el.getAttribute('data-t-aria') || el.getAttribute('aria-label') || '';
      if (!el.hasAttribute('data-t-aria')) el.setAttribute('data-t-aria', key);
      el.setAttribute('aria-label', t(key));
    });
  }

  /* The chosen language is a cookie, not localStorage: the server needs it
     before the page is rendered, or the first paint is in the wrong tongue. */
  function setLocale(code) {
    document.cookie = 'fs_lang=' + encodeURIComponent(code) +
      ';path=/;max-age=31536000;SameSite=Lax';
    location.reload();
  }

  window.T = t;
  window.FS_I18N = {
    locale: C.locale || 'en',
    dir: C.dir || 'ltr',
    messages: C.messages || {},
    locales: C.locales || [],
    t: t,
    apply: apply,
    setLocale: setLocale
  };
})();
