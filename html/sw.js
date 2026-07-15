const CACHE_NAME = 'e3dc-control-local-vendor-r0-e5-20260712';
const ASSETS = [
  'assets/vendor/bootstrap/css/bootstrap.min.css',
  'assets/vendor/bootstrap/js/bootstrap.bundle.min.js',
  'assets/vendor/bootstrap-icons/bootstrap-icons.min.css',
  'assets/vendor/bootstrap-icons/fonts/bootstrap-icons.woff2',
  'assets/vendor/chart.js/chart.umd.min.js',
  'assets/vendor/chartjs-plugin-zoom/chartjs-plugin-zoom.min.js',
  'assets/vendor/fontawesome/css/all.min.css',
  'assets/vendor/fontawesome/webfonts/fa-brands-400.ttf',
  'assets/vendor/fontawesome/webfonts/fa-brands-400.woff2',
  'assets/vendor/fontawesome/webfonts/fa-regular-400.ttf',
  'assets/vendor/fontawesome/webfonts/fa-regular-400.woff2',
  'assets/vendor/fontawesome/webfonts/fa-solid-900.ttf',
  'assets/vendor/fontawesome/webfonts/fa-solid-900.woff2',
  'assets/vendor/fontawesome/webfonts/fa-v4compatibility.ttf',
  'assets/vendor/fontawesome/webfonts/fa-v4compatibility.woff2',
  'assets/vendor/hammerjs/hammer.min.js',
  'assets/vendor/jquery/jquery-3.6.0.min.js'
];

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    await cache.addAll(ASSETS);
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key)));
    await self.clients.claim();
  })());
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

