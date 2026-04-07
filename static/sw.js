const CACHE = 'toledo-v3';
const SHELL = [
  '/',
  '/static/index.html?v=3',
  '/manifest.json',
  '/static/icon-192.svg',
  '/static/icon-512.svg'
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/api/')) {
    // Network-first for API calls
    e.respondWith(
      fetch(e.request).catch(() =>
        new Response(JSON.stringify({ error: 'offline' }), {
          headers: { 'Content-Type': 'application/json' }
        })
      )
    );
  } else {
    // Cache-first for app shell
    e.respondWith(
      caches.match(e.request).then(r => r || fetch(e.request))
    );
  }
});
