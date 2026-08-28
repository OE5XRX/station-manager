// Registers this browser for Web-Push and POSTs the subscription.
(function () {
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

  btn.addEventListener('click', async function () {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      status.textContent = 'Push is not supported on this browser.';
      return;
    }
    try {
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') { status.textContent = 'Permission denied.'; return; }
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
      status.textContent = res.ok ? 'Push enabled on this device.' : 'Registration failed.';
      if (res.ok) setTimeout(() => location.reload(), 800);
    } catch (e) {
      status.textContent = 'Could not enable push: ' + e.message;
    }
  });
})();
