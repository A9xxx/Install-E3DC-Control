const CACHE_NAME = 'e3dc-control-v5-4-2c-static';
const ASSETS = [
  'assets/vendor/bootstrap/css/bootstrap.min.css',
  'assets/vendor/fontawesome/css/all.min.css',
  'assets/vendor/bootstrap/js/bootstrap.bundle.min.js',
  'assets/vendor/chart.js/chart.umd.min.js',
  'assets/vendor/hammerjs/hammer.min.js',
  'assets/vendor/chartjs-plugin-zoom/chartjs-plugin-zoom.min.js'
];

self.addEventListener('install', event => {
  self.skipWaiting();

  caches.open(CACHE_NAME).then(cache => {
    cache.addAll(ASSETS).catch(e => console.warn("Cache fail:", e));
  }).catch(e => console.warn("Cache-Open fail:", e));
});

self.addEventListener('activate', event => {
  self.clients.claim();

  caches.keys().then(keys => {
    keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key));
  }).catch(e => console.warn("Cleanup fail:", e));
});

self.addEventListener('message', event => {
  if (!event.data || event.data.type !== 'SKIP_WAITING') {
    return;
  }
  self.skipWaiting();
});

self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') {
    return;
  }

  const requestUrl = new URL(event.request.url);
  const path = requestUrl.pathname;

  if (
    requestUrl.origin === self.location.origin ||
    path.includes('.php') ||
    path.includes('awattardebug') ||
    path.includes('diagramm_mobile') ||
    path.includes('status')
  ) {
    event.respondWith(
      fetch(event.request).catch(() => caches.match(event.request))
    );
    return;
  }

  event.respondWith(
    caches.match(event.request).then(response => {
      return response || fetch(event.request);
    })
  );
});

// --- Web Push Handling ---

self.addEventListener('push', event => {
  if (!event.data) return;

  try {
    const data = event.data.json();
    const title = data.title || 'E3DC-Control Notifikation';
    const options = {
      body: data.body || '',
      icon: 'app-icon-192.png',
      badge: 'app-icon-192.png',
      vibrate: [200, 100, 200],
      data: {
        url: data.url || '/'
      }
    };
    if (data.actions) {
      options.actions = data.actions;
    }

    event.waitUntil(
      self.registration.showNotification(title, options)
    );
  } catch (e) {
    console.error('Push payload parse error', e);
    event.waitUntil(
      self.registration.showNotification('E3DC-Control', {
        body: event.data.text(),
        icon: 'app-icon-192.png'
      })
    );
  }
});

self.addEventListener('notificationclick', event => {
  event.notification.close();

  if (event.action) {
    const actionUrl = '/webhook.php?action=' + encodeURIComponent(event.action);
    event.waitUntil(
      fetch(actionUrl).then(response => response.json()).then(result => {
        console.log("Action triggered:", result);
      }).catch(e => console.error("Action error:", e))
    );
    return;
  }

  const targetUrl = event.notification.data ? event.notification.data.url : '/';

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
      for (const client of clientList) {
        if (client.url.includes(targetUrl) && 'focus' in client) {
          return client.focus();
        }
      }
      if (clients.openWindow) {
        return clients.openWindow(targetUrl);
      }
    })
  );
});
