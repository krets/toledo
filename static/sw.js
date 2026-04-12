const CACHE = 'toledo-v5'; // Incremented version
const SHELL = [
  '/',
  '/static/index.html?v=5',
  '/manifest.json',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/favicon.ico',
  '/static/apple-touch-icon.png'
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
    // Network-only for API calls to ensure auth state is always fresh
    // We remove the .catch() so that 401s and network errors propagate to the app
    e.respondWith(fetch(e.request));
  } else {
    // Cache-first for app shell
    e.respondWith(
      caches.match(e.request).then(r => r || fetch(e.request))
    );
  }
});
