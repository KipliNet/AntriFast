const CACHE_NAME = "antri-lokal-v2";

const PRECACHE = [
    "/",
    "/offline",
    "/static/manifest.json"
];

// Install Event
self.addEventListener("install", (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => {
            return cache.addAll(PRECACHE);
        })
    );
    self.skipWaiting();
});

// Activate Event - hapus cache lama
self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys().then(keys => {
            return Promise.all(
                keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
            );
        })
    );
    self.clients.claim();
});

// Fetch Handler
self.addEventListener("fetch", (event) => {
    const req = event.request;

    // Fokus pada halaman HTML
    if (req.mode === "navigate") {
        event.respondWith(
            fetch(req).catch(() => caches.match("/offline"))
        );
        return;
    }

    // Stale-while-revalidate untuk asset & halaman umum
    event.respondWith(
        caches.match(req).then(cacheRes => {
            return (
                cacheRes ||
                fetch(req)
                    .then(fetchRes => {
                        return caches.open(CACHE_NAME).then(cache => {
                            cache.put(req, fetchRes.clone());
                            return fetchRes;
                        });
                    })
            );
        })
    );
});
