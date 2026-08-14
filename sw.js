/**
 * sw.js — Service Worker
 *
 * 修复说明（2026-08-14）：
 * 原实现是「缓存优先（cache-first）」且缓存名固定为 asset-dash-v1，从未失效。
 * 导致浏览器一旦命中旧缓存，就永远显示旧数据——即使 GitHub 上的
 * Asset_parsed.json 已经更新（例如已卖出的股票仍显示在页面上）。
 *
 * 本次修复：
 *  1. 动态数据（.json，即 Asset_parsed.json）改为「网络优先」，
 *     每次打开都先从服务器拉最新数据，失败才回退缓存 → 根治「卖了还显示」。
 *  2. 页面（/ 与 index.html）同样网络优先，保证部署更新及时生效。
 *  3. 静态资源（图标等）仍缓存优先，保留 PWA 离线能力。
 *  4. 缓存版本提升到 asset-dash-v2，并在 activate 时删除所有旧版本缓存，
 *     立即清除已经污染的旧数据。
 *  5. 保留 skipWaiting + clients.claim，让新 Service Worker 立即接管。
 */
const CACHE = 'asset-dash-v2';

/** 需要「始终拉最新」的资源：网络优先，缓存仅作离线兜底 */
const NETWORK_FIRST = ['.json', '/'];

self.addEventListener('install', function (e) {
  self.skipWaiting();
  e.waitUntil(
    caches.open(CACHE).then(function (c) {
      return c.add('./');
    }).catch(function () {})
  );
});

self.addEventListener('activate', function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      // 删除所有旧版本缓存，避免污染数据继续被命中
      return Promise.all(
        keys.filter(function (k) { return k !== CACHE; })
            .map(function (k) { return caches.delete(k); })
      );
    }).then(function () {
      return self.clients.claim();
    })
  );
});

self.addEventListener('fetch', function (e) {
  if (e.request.method !== 'GET') return;

  var url = e.request.url;
  var isPage = url.indexOf('/index.html') !== -1 ||
               url.replace(/\/+$/, '') === self.location.origin.replace(/\/+$/, '');
  var needsFresh = NETWORK_FIRST.some(function (s) {
    return url.indexOf(s) !== -1;
  }) || isPage;

  if (needsFresh) {
    // 网络优先：先请求服务器，成功则更新缓存；失败才回退缓存
    e.respondWith(
      fetch(e.request).then(function (resp) {
        if (resp && resp.status === 200 && resp.type !== 'opaque') {
          var cp = resp.clone();
          caches.open(CACHE).then(function (c) { c.put(e.request, cp); });
        }
        return resp;
      }).catch(function () {
        return caches.match(e.request).then(function (r) {
          return r || caches.match('./');
        });
      })
    );
    return;
  }

  // 静态资源：缓存优先
  e.respondWith(
    caches.match(e.request).then(function (r) {
      if (r) return r;
      return fetch(e.request).then(function (resp) {
        if (resp && resp.status === 200 && resp.type !== 'opaque') {
          var cp = resp.clone();
          caches.open(CACHE).then(function (c) { c.put(e.request, cp); });
        }
        return resp;
      }).catch(function () {
        return caches.match('./');
      });
    })
  );
});
