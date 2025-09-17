const CACHE_NAME = 'orders-v1';
const urlsToCache = [
    '/',
    '/index.html',
    '/style.css',
    '/script.js',
    '/manifest.json',
    '/icon-192.png',
    '/icon-512.png'
];

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then(cache => cache.addAll(urlsToCache))
    );
});

self.addEventListener('fetch', event => {
    event.respondWith(
        caches.match(event.request)
            .then(response => {
                if (response) {
                    return response;
                }
                return fetch(event.request).catch(() => {
                    // Возвращаем кэшированную страницу для офлайн-режима
                    if (event.request.mode === 'navigate') {
                        return caches.match('/');
                    }
                });
            })
    );
});

self.addEventListener('sync', event => {
    if (event.tag === 'sync-orders') {
        event.waitUntil(syncOfflineOrders());
    }
});

async function syncOfflineOrders() {
    const offlineOrders = JSON.parse(localStorage.getItem('offline_orders') || '[]');
    if (offlineOrders.length > 0) {
        for (const order of offlineOrders) {
            try {
                const response = await fetch('/api/sync_order', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(order)
                });
                if (response.ok) {
                    // Удаляем из офлайн-хранилища
                    const index = offlineOrders.indexOf(order);
                    if (index > -1) {
                        offlineOrders.splice(index, 1);
                        localStorage.setItem('offline_orders', JSON.stringify(offlineOrders));
                    }
                }
            } catch (error) {
                console.error('Ошибка синхронизации:', error);
            }
        }
    }
}