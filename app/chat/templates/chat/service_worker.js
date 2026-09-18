self.addEventListener('push', event => {
    const data = event.data ? event.data.json() : {};
    event.waitUntil(self.registration.showNotification(data.title || 'Vobia Space', {
        body: data.body || 'Ada aktivitas baru di Vobia Space.',
        icon: '/static/img/vobia-tiktok-app-icon.png?v=20260831',
        badge: '/static/img/vobia-tiktok-app-icon.png?v=20260831',
        tag: data.tag || 'vobia-space',
        data: {url: data.url || '/'},
    }));
});

self.addEventListener('notificationclick', event => {
    event.notification.close();
    const target = new URL(event.notification.data?.url || '/', self.location.origin).href;
    event.waitUntil((async () => {
        const windows = await clients.matchAll({type: 'window', includeUncontrolled: true});
        const existing = windows.find(client => client.url.startsWith(self.location.origin));
        if (existing) {
            await existing.focus();
            return existing.navigate(target);
        }
        return clients.openWindow(target);
    })());
});
