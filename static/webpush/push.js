// Registers this browser for Web-Push and POSTs the subscription.
(function () {
  // i18n: globals from Django's JavaScriptCatalog; identity fallback if it failed to load.
  const gettext = globalThis.gettext || ((s) => s);
  const interpolate = globalThis.interpolate ||
    ((fmt, obj) => fmt.replace(/%\(\s*([^)]+)\s*\)s/g, (m, k) => String(obj[k])));
  const section = document.getElementById('push-section');
  const btn = document.getElementById('enable-push');
  const status = document.getElementById('push-status');
  if (!section || !btn) return;

  function getCookie(name) {
    const m = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)');
    return m ? m.pop() : '';
  }

  function urlB64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(base64);
    return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
  }

  document.addEventListener('click', async function (e) {
    const removeBtn = e.target.closest('.js-remove-device');
    if (!removeBtn) return;
    if (!section) return;
    const row = removeBtn.closest('[data-endpoint]');
    if (!row) return;
    const endpoint = row.dataset.endpoint;
    try {
      const res = await fetch(section.dataset.unsubscribeUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCookie('csrftoken') },
        body: JSON.stringify({ endpoint }),
      });
      if (!res.ok) { status.textContent = gettext('Could not remove device.'); return; }
      // Best-effort: unsubscribe browser-side if this endpoint matches current subscription.
      if ('serviceWorker' in navigator) {
        try {
          const reg = await navigator.serviceWorker.ready;
          const sub = await reg.pushManager.getSubscription();
          if (sub && sub.endpoint === endpoint) await sub.unsubscribe();
        } catch (_) { /* ignore */ }
      }
      row.remove();
      const removed = gettext('Device removed.');
      status.textContent = removed;
      setTimeout(() => { if (status.textContent === removed) status.textContent = ''; }, 3000);
    } catch (e) {
      status.textContent = interpolate(gettext('Could not remove device: %(error)s'), { error: e.message }, true);
    }
  });

  btn.addEventListener('click', async function () {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      status.textContent = gettext('Push is not supported on this browser.');
      return;
    }
    try {
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') { status.textContent = gettext('Permission denied.'); return; }
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlB64ToUint8Array(section.dataset.vapidKey),
      });
      const res = await fetch(section.dataset.subscribeUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCookie('csrftoken') },
        body: JSON.stringify(sub.toJSON()),
      });
      status.textContent = res.ok ? gettext('Push enabled on this device.') : gettext('Registration failed.');
      if (res.ok) setTimeout(() => location.reload(), 800);
    } catch (e) {
      status.textContent = interpolate(gettext('Could not enable push: %(error)s'), { error: e.message }, true);
    }
  });
})();
