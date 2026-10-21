// Service worker minimal : permet l'installation sur l'écran d'accueil.
// Les données (/api) ne sont jamais mises en cache : les prix doivent toujours être frais.
const CACHE = "collection-v1";
const SHELL = ["/", "style.css", "app.js", "icon.svg", "manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim())
  );
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  // réseau d'abord, cache en secours (hors ligne)
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
