'use strict';
const $=id=>document.getElementById(id);
const API='https://sportscore.com/api/widget';
let history=[],seen=new Set(),busy=false,notifyOn=false,audioCtx=null;
try{history=JSON.parse(localStorage.getItem('gr_history')||'[]')}catch{}
try{seen=new Set(JSON.parse(localStorage.getItem('gr_seen')||'[]'))}catch{}
notifyOn=localStorage.getItem('gr_notify')==='1';
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function state(ok,label){$('state').textContent=label;$('dot').style.background=ok?'#42e6a2':'#ffbd59'}
async function get(url){const ctl=new AbortController(),timer=setTimeout(()=>ctl.abort(),12000);try{const r=await fetch(url,{cache:'no-store',signal:ctl.signal});if(!r.ok)throw Error('HTTP '+r.status);return await r.json()}finally{clearTimeout(timer)}}
function deep(o,keys,d=0){if(!o||typeof o!=='object'||d>6)return null;for(const k of keys)if(o[k]!==undefined&&o[k]!==null&&o[k]!=='')return o[k];for(const v of Object.values(o)){if(v&&typeof v==='object'){const x=deep(v,keys,d+1);if(x!==null)return x}}return null}
function clockSeconds(v){if(v===null||v===undefined)return null;if(typeof v==='number')return v>=0&&v<9000?Math.round(v*60):null;const s=String(v).trim();let m=s.match(/^(\d{1,3}):([0-5]\d)$/);if(m)return Number(m[1])*60+Number(m[2]);m=s.match(/(\d{1,3})(?:\s*['’]|\s*min)?/i);if(m){const n=Number(m[1]);if(n>=0&&n<=130)return n*60}return null}
function normalize(x,assumeLive=false){
const st=x.status??x.state??x.match_status??x.matchStatus??x.status_display??x.statusDisplay??x.status_detail??x.statusDetail??'';
const stat=typeof st==='object'?String(st.short??st.long??st.description??st.name??st.type?.name??st.type?.state??st.state??''):String(st);
const statusText=String(x.status_text??x.statusText??x.status_display??x.statusDisplay??x.status_detail??x.statusDetail??x.period_name??'');
const phaseText=(stat+' '+statusText+' '+String(x.period??x.half??'')).toLowerCase();
const done=/finished|full.?time|(^|\W)ft($|\W)|ended|complete|cancelled|canceled|postponed|abandoned/.test(phaseText);
const future=/not.?started|scheduled|upcoming|fixture|pre.?match|(^|\W)ns($|\W)/.test(phaseText);
let raw=deep(x,['elapsed_seconds','elapsedSeconds','elapsed','elapsed_time','elapsedTime','current_minute','currentMinute','minute','match_clock','matchClock','clock','timer','display_time','displayTime','time_elapsed','elapsed_minutes']);
let seconds=clockSeconds(raw);if(seconds===null&&/[0-9]{1,3}\s*['’]|[0-9]{1,3}\s*min/i.test(statusText))seconds=clockSeconds(statusText);
const liveFlag=x.is_live===true||x.live===true||x.isLive===true||x.status?.type?.state==='in'||x.status?.state==='in'||String(x.status).toLowerCase()==='inprogress';
const liveText=/live|in.?play|in.?progress|playing|1st half|2nd half|first half|second half|half.?time|(^|\W)1h($|\W)|(^|\W)2h($|\W)|(^|\W)ht($|\W)|45\+|90\+|devre|oynan/.test(phaseText);
if(done||future||(!liveFlag&&!liveText))return null;
let home=x.home??x.home_team??x.home_name??x.teams?.home??x.competitors?.[0]??null;
let away=x.away??x.away_team??x.away_name??x.teams?.away??x.competitors?.[1]??null;
if(typeof home==='object')home=home.name??home.display_name??home.short_name??home.team?.name??null;
if(typeof away==='object')away=away.name??away.display_name??away.short_name??away.team?.name??null;
if(!home||!away){const title=String(x.title??x.name??x.event_name??'');const pair=title.split(/\s+vs\.?\s+|\s+v\s+/i);if(pair.length===2){home=home||pair[0].trim();away=away||pair[1].trim()}}
if(!home||!away)return null;
const score=v=>v&&typeof v==='object'?Number(v.current??v.display??v.goals??v.score??0)||0:Number(v??0)||0;
const slug=x.slug??x.match_slug??x.url_slug??null;const id=String(x.id??x.match_id??x.event_id??slug??[String(home).trim().toLowerCase(),String(away).trim().toLowerCase(),String(x.competition_name??x.competition?.name??x.league?.name??'')].join('|'));return {raw:x,id,slug,home,away,hs:score(x.home_score??x.home_goals??x.home_team?.score??x.scores?.home??x.score?.home??x.score?.home_score),as:score(x.away_score??x.away_goals??x.away_team?.score??x.scores?.away??x.score?.away??x.score?.away_score),seconds,clockBase:Date.now(),period:String(x.period_name??x.period??x.half??statusText??stat),status:stat.toLowerCase(),statusText,league:x.competition_name??x.competition?.name??x.competition??x.league?.name??x.league??'Canlı maç'}
}
function phase(m){const s=(m.status+' '+m.statusText+' '+m.period).toLowerCase();if(/half.?time|\bht\b|devre/.test(s))return 'DEVRE';if(/2nd half|second half|\b2h\b|2. yar/.test(s))return '2. YARI';if(/1st half|first half|\b1h\b|1. yar/.test(s))return '1. YARI';return 'CANLI'}
function elapsed(m){if(m.seconds===null)return 'SÜRE BİLGİSİ YOK';const total=Math.max(0,m.seconds+Math.floor((Date.now()-m.clockBase)/1000));return Math.floor(total/60)+':'+String(total%60).padStart(2,'0')}
function statsWalk(o,out={shots:0,on:0,corners:0,xg:0,attacks:0},depth=0){if(!o||typeof o!=='object'||depth>7)return out;if(Array.isArray(o)){for(const v of o)statsWalk(v,out,depth+1);return out}for(const [k,v] of Object.entries(o)){const key=k.toLowerCase().replace(/[^a-z]/g,'');if(typeof v==='number'||typeof v==='string'){const n=Number(String(v).replace('%',''));if(!Number.isFinite(n))continue;if(/shot.*target|target.*shot|ontarget/.test(key))out.on=Math.max(out.on,n);else if(/corner/.test(key))out.corners=Math.max(out.corners,n);else if(/expectedgoals|^xg$/.test(key))out.xg=Math.max(out.xg,n);else if(/totalshots|shots|shotattempt/.test(key))out.shots=Math.max(out.shots,n);else if(/dangerousattack|attacks/.test(key))out.attacks=Math.max(out.attacks,n)}else if(v&&typeof v==='object'){const label=String(v.name??v.label??v.type??k).toLowerCase();const val=v.value??v.total??v.home??v.home_value??v.displayValue;if(val!==undefined)statsWalk({[label]:val},out,depth+1);statsWalk(v,out,depth+1)}}return out}
async function matchDetail(m){if(!m.slug)return null;try{return await get(API+'/match/?sport=football&slug='+encodeURIComponent(m.slug))}catch{return null}}
function analyze(m,d){const s=statsWalk(d||m.raw);const points=s.on*11+s.shots*2+s.corners*3+s.xg*16+s.attacks*.08;const has=s.on+s.shots+s.corners+s.xg+s.attacks>0;const score=has?Math.min(99,Math.round(points)):null;let level='VERİ BEKLENİYOR';if(has)level=score>=85?'ÇOK YÜKSEK BASKI':score>=65?'YÜKSEK BASKI':score>=40?'ORTA BASKI':'DÜŞÜK BASKI';return {s,score,has,level}}
function card(m,a,signal){const clock=elapsed(m),cls=signal?'match signal':'match';let desc=signal?'BASKI '+a.score+'/100 · İsabetli şut '+a.s.on+' · Şut '+a.s.shots+' · Korner '+a.s.corners:(a.has?a.level+' · '+a.score+'/100':'İstatistik akışı bekleniyor');return '<article class="'+cls+'"><div class="match-top"><span class="league">'+esc(m.league)+'</span><span class="phase">'+phase(m)+'</span><span class="'+(signal?'signal-tag':'live-tag')+'">'+(signal?'⚡ GOL SİNYALİ':'● CANLI')+'</span></div><div class="match-main"><div class="team"><b>'+esc(m.home)+'</b><small>EV SAHİBİ</small></div><div class="scoreboard"><strong>'+m.hs+' : '+m.as+'</strong><small>'+clock+'</small></div><div class="team away"><b>'+esc(m.away)+'</b><small>DEPLASMAN</small></div></div><div class="match-foot"><span>'+esc(m.statusText||m.status||'Canlı')+'</span><span class="analysis">'+desc+'</span></div></article>'}
function signalIdentity(m){return String(m.slug||m.id||[m.home,m.away,m.league].join('|')).toLowerCase()}
function playSignal(m,a){
const msg=m.home+' ile '+m.away+' maçında güçlü gol baskısı tespit edildi. Skor '+m.hs+'-'+m.as+'. '+(elapsed(m)==='SÜRE BİLGİSİ YOK'?'':elapsed(m)+' dakika. ')+'Baskı puanı '+a.score+' üzerinden 100.';
if('Notification'in window&&Notification.permission==='granted')new Notification('GOL RADAR · Güçlü gol sinyali',{body:msg,icon:'./icon.svg',tag:signalIdentity(m)});
if(notifyOn&&'speechSynthesis'in window){try{speechSynthesis.cancel();const u=new SpeechSynthesisUtterance(msg);u.lang='tr-TR';u.rate=1;u.pitch=1;speechSynthesis.speak(u)}catch{}}
}
function saveSignal(m,a){const key=signalIdentity(m);if(seen.has(key))return;seen.add(key);localStorage.setItem('gr_seen',JSON.stringify([...seen].slice(-500)));history.unshift({match:m.home+' – '+m.away,score:a.score,time:new Date().toLocaleTimeString('tr-TR',{hour:'2-digit',minute:'2-digit'})});history=history.slice(0,80);localStorage.setItem('gr_history',JSON.stringify(history));if(notifyOn)playSignal(m,a)}
function renderHistory(){$('total').textContent=history.length;$('history').innerHTML=history.length?history.slice(0,10).map(x=>'<div class="historyitem"><span>'+esc(x.match)+' · '+esc(x.time)+'</span><b>'+x.score+'/100</b></div>').join(''):'Henüz sinyal yok.'}
function uniqueMatches(list){const seenKeys=new Set();return list.filter(m=>{const clean=v=>String(v??'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').replace(/[^a-z0-9]+/gi,' ').trim().toLowerCase();const key=[clean(m.league),clean(m.home),clean(m.away)].join('|');if(!clean(m.home)||!clean(m.away)||seenKeys.has(key))return false;seenKeys.add(key);return true}).sort((a,b)=>String(a.league).localeCompare(String(b.league),'tr')||String(a.home).localeCompare(String(b.home),'tr')||String(a.away).localeCompare(String(b.away),'tr'))}
function extractRows(d,out=[]){if(!d)return out;if(Array.isArray(d)){for(const v of d)extractRows(v,out);return out}if(typeof d!=='object')return out;
const hasTeams=(d.home||d.home_team||d.teams?.home||d.competitors?.length>=2||/ vs\.? /i.test(String(d.title??'')))&&(d.away||d.away_team||d.teams?.away||d.competitors?.length>=2||/ vs\.? /i.test(String(d.title??'')));
if(hasTeams&&(d.status!==undefined||d.state!==undefined||d.is_live!==undefined||d.live!==undefined||d.timer!==undefined||d.elapsed!==undefined)){out.push(d);return out}
for(const [k,v] of Object.entries(d)){if(v&&typeof v==='object')extractRows(v,out)}return out}

async function refresh(){if(busy)return;busy=true;$('refresh').disabled=true;state(false,'CANLI VERİ ALINIYOR');try{
const utcDate=new Date().toISOString().slice(0,10);
const source='SportScore doğrulanmış canlı fikstür';
const data=await get('https://sportscore.com/api/v1/fixtures/?sport=football&date='+utcDate+'&status=live&limit=200');
const rows=extractRows(data);
const matches=uniqueMatches(rows.map(x=>normalize(x,false)).filter(Boolean));
const items=matches.map(m=>({m,a:analyze(m,m.raw)}));
function paint(list){const signals=list.filter(x=>x.a.has&&x.a.score>=85).sort((a,b)=>b.a.score-a.a.score);
$('signals').innerHTML=signals.length?signals.map(x=>card(x.m,x.a,true)).join(''):'<div class="empty signal-empty"><b>Şu anda güçlü gol sinyali yok</b><span>İstatistik verisi gelen ve 85/100 eşiğini geçen maçlar burada gösterilir.</span></div>';
$('matches').innerHTML='';
$('liveCount').textContent=list.length;$('signalCount').textContent=signals.length;$('analysisCount').textContent=list.filter(x=>x.a.has).length;$('scanTime').textContent=new Date().toLocaleTimeString('tr-TR',{hour:'2-digit',minute:'2-digit',second:'2-digit'});for(const x of signals)saveSignal(x.m,x.a);renderHistory();}
paint(items);$('message').textContent=source+' · '+rows.length+' kayıt kontrol edildi · '+matches.length+' tekil canlı maç.';$('message').classList.remove('error');state(true,'CANLI TAKİP AKTİF');
const detailTargets=matches.slice(0,10);const details=await Promise.all(detailTargets.map(m=>matchDetail(m)));
const detailMap=new Map(detailTargets.map((m,i)=>[signalIdentity(m),details[i]]));const full=items.map(x=>({m:x.m,a:detailMap.get(signalIdentity(x.m))?analyze(x.m,detailMap.get(signalIdentity(x.m))):x.a})).filter(x=>x.a.has&&x.a.score>=85).sort((a,b)=>b.a.score-a.a.score);
paint(full);$('message').textContent=source+' · '+full.length+' güçlü sinyal · yalnızca istatistik verisi yeterli maçlar gösteriliyor.';
}catch(e){$('message').textContent='Canlı veri bağlantısı hatası: '+esc(e.message);$('message').classList.add('error');$('signals').innerHTML='<div class="empty">Veri sağlayıcısı yanıt vermiyor.</div>';$('matches').innerHTML='<div class="empty">Canlı maçlar şu anda alınamıyor.</div>';state(false,'VERİ BAĞLANTISI YOK')}finally{busy=false;$('refresh').disabled=false}}
$('refresh').addEventListener('click',refresh);
$('clear').addEventListener('click',()=>{if(confirm('Sinyal geçmişi temizlensin mi?')){history=[];localStorage.removeItem('gr_history');renderHistory()}});
$('notify').addEventListener('click',async()=>{if(!('Notification'in window)){alert('Bu tarayıcı bildirimleri desteklemiyor.');return}if(Notification.permission!=='granted'){const p=await Notification.requestPermission();notifyOn=p==='granted'}else notifyOn=!notifyOn;if(notifyOn){try{const AC=window.AudioContext||window.webkitAudioContext;if(AC){audioCtx=audioCtx||new AC();if(audioCtx.state==='suspended')await audioCtx.resume()}if('speechSynthesis'in window){const u=new SpeechSynthesisUtterance('GOL RADAR sesli bildirimleri açıldı.');u.lang='tr-TR';speechSynthesis.speak(u)}}catch{}}localStorage.setItem('gr_notify',notifyOn?'1':'0');$('notify').textContent=notifyOn?'✓ Sesli + yazılı açık':'🔔 Sesli + yazılı uyarı';});
$('notify').textContent=notifyOn?'✓ Sesli + yazılı açık':'🔔 Sesli + yazılı uyarı';
if('serviceWorker'in navigator)window.addEventListener('load',()=>navigator.serviceWorker.register('./sw.js').catch(()=>{}));
renderHistory();refresh();setInterval(()=>refresh(),120000);