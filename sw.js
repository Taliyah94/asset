const CACHE='asset-dash-v1';
self.addEventListener('install', function(e){ self.skipWaiting(); e.waitUntil(caches.open(CACHE).then(function(c){ return c.add('./'); }).catch(function(){})); });
self.addEventListener('activate', function(e){ e.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', function(e){
  if(e.request.method!=='GET') return;
  e.respondWith(caches.match(e.request).then(function(r){
    if(r) return r;
    return fetch(e.request).then(function(resp){
      if(resp && resp.status===200 && resp.type!=='opaque'){
        var cp=resp.clone(); caches.open(CACHE).then(function(c){ c.put(e.request, cp); });
      }
      return resp;
    }).catch(function(){ return caches.match('./'); });
  }));
});