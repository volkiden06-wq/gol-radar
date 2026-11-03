const C='gol-radar-cache-v21';
const A=['./','./index.html','./style.css','./app.js','./manifest.webmanifest','./icon.svg'];
self.addEventListener('install',e=>e.waitUntil(caches.open(C).then(c=>c.addAll(A)).then(()=>self.skipWaiting())));
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(k=>Promise.all(k.filter(x=>x.startsWith('gol-radar-cache-')&&x!==C).map(x=>caches.delete(x)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',e=>{
 if(new URL(e.request.url).origin!==location.origin||e.request.method!=='GET')return;
 const u=new URL(e.request.url);
 const fresh=['.','/','/index.html','/app.js','/style.css','/sw.js'].includes(u.pathname);
 if(fresh){
  e.respondWith(fetch(e.request,{cache:'no-store'}).then(r=>{if(r.ok){const copy=r.clone();caches.open(C).then(c=>c.put(e.request,copy))}return r}).catch(()=>caches.match(e.request).then(r=>r||caches.match('./index.html'))));
 }else{
  e.respondWith(caches.match(e.request).then(r=>r||fetch(e.request).then(n=>{if(n.ok){const copy=n.clone();caches.open(C).then(c=>c.put(e.request,copy))}return n})));
 }
});