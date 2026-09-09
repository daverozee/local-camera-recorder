'use strict';
const $=s=>document.querySelector(s);
let state=null, token='', offset=0, total=0, loading=false;
const cameraActions=new Set();
const size=n=>n>=1024**3?(n/1024**3).toFixed(1)+' GB':n>=1024**2?(n/1024**2).toFixed(1)+' MB':(n/1024).toFixed(0)+' KB';
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url,data) {
  const options=data?{method:'POST',headers:{'Content-Type':'application/json','X-LocalCam-Token':token},body:JSON.stringify(data)}:{};
  const response=await fetch(url,options);const result=await response.json();
  if(!response.ok)throw new Error(result.error||'Request failed');return result;
}
function banner(message=''){$('#banner').textContent=message;$('#banner').classList.toggle('hidden',!message);}
function renderCameras() {
  const statusNames={stopped:'NOT RECORDING',connecting:'CONNECTING',recording:'RECORDING',reconnecting:'RECONNECTING',paused:'PAUSED',error:'NEEDS ATTENTION'};
  $('#cameras').innerHTML=state.cameras.map(c=>`<article class="camera-card"><div class="camera-visual"><span class="status-pill ${esc(c.status)}">${statusNames[c.status]||esc(c.status)}</span><div class="camera-symbol" aria-hidden="true"></div><span class="visual-caption">${c.running?'Recording status · no live preview':'Ready when you are'}</span></div><div class="camera-content"><div class="camera-topline"><h3 class="camera-name">${esc(c.name)}</h3><button class="edit-button" data-edit="${esc(c.id)}" ${c.running?'disabled':''}>Edit ↗</button></div><p class="camera-address">${esc(c.host)}:${c.port} / ${esc(c.stream)}</p><div class="camera-message">${esc(c.message)}</div><button class="button ${c.running?'secondary':'primary'}" data-action="${c.running?'stop':c.has_password?'start':'setup'}" data-camera="${esc(c.id)}">${c.running?'■ Stop recording':c.has_password?'● Start recording':'＋ Connect camera'}</button><div class="ptz-controls"><div class="ptz-heading"><span>Pan & tilt</span><button class="text-button" data-ptz="${esc(c.id)}" ${!c.has_password?'disabled':''}>Check support</button></div><label class="checkbox"><input type="checkbox" data-sweep="${esc(c.id)}" ${c.sweep_enabled?'checked':''} ${!c.sweep_enabled&&!(c.ptz_maps||[]).length?'disabled':''}> Sweep while recording</label>${(c.ptz_maps||[]).length?`<select aria-label="Cruise track for ${esc(c.name)}" data-track="${esc(c.id)}">${c.ptz_maps.map(m=>`<option value="${esc(m)}" ${m===c.sweep_map?'selected':''}>${esc(m)}</option>`).join('')}</select>`:''}<p class="form-help ${c.sweep_status==='error'?'ptz-error':''}">${esc(c.sweep_message||'Check support after saving camera credentials.')}</p>${c.sweep_enabled?'<small>20-second passes · 10-second pauses</small>':''}</div></div></article>`).join('');
  const chosen=$('#filter-camera').value;
  $('#filter-camera').innerHTML='<option value="">All cameras</option>'+state.cameras.map(c=>`<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');
  $('#filter-camera').value=chosen;
}
async function refreshState() {
  if(loading)return;loading=true;
  try {
    state=await api('/api/state');token=state.token;
    renderDiscovery();
    $('#stat-recording').textContent=state.cameras.filter(c=>c.status==='recording').length;
    $('#stat-connected').textContent=`Of ${state.cameras.length} cameras`;
    $('#stat-clips').textContent=state.clips.toLocaleString();$('#stat-size').textContent=size(state.bytes);
    $('#stat-free').textContent=size(state.free_bytes)+' free on drive';
    $('#stat-folder').textContent=state.settings.storage_root;
    $('#stat-retention').textContent=state.settings.cleanup_enabled?`Clips kept for ${state.settings.retention_days} days`:'Automatic cleanup is off';
    $('#camera-count').textContent=state.cameras.length;if(!cameraActions.size)renderCameras();
    if(!state.ffmpeg_available)banner('FFmpeg is missing. Run setup.ps1 and restart the app.');
    else banner(state.maintenance_error||'');
  }catch(e){banner(e.message);}finally{loading=false;}
}
async function refreshClips() {
  try {
    const query=new URLSearchParams({camera:$('#filter-camera').value,date:$('#filter-date').value,offset});
    const result=await api('/api/clips?'+query);total=result.total;
    $('#clip-count').textContent=total?`${total.toLocaleString()} clips`:'';
    if(!result.items.length) {
      $('#library').innerHTML=`<div class="empty-library"><div class="empty-icon">▱</div><h3>${total?'No more recordings':($('#filter-camera').value||$('#filter-date').value)?'No clips match these filters':'Your archive starts here'}</h3><p>${state&&state.clips?'Choose another camera or date to find your recordings.':'Connect a camera and start recording. Your first clip will appear after about a minute.'}</p></div>`;
    } else {
      $('#library').innerHTML='<table><thead><tr><th>CAMERA</th><th>RECORDED AT · LOCAL TIME</th><th>LENGTH</th><th>SIZE</th><th></th></tr></thead><tbody>'+result.items.map(c=>{
        const name=state?.cameras.find(x=>x.id===c.camera)?.name||c.camera;
        return `<tr><td>${esc(name)}</td><td>${esc(new Date(c.start*1000).toLocaleString())}</td><td>${Math.round(c.duration)} sec</td><td>${size(c.size)}</td><td><button class="button secondary compact" data-play="${esc(c.id)}" data-title="${esc(name+' · '+new Date(c.start*1000).toLocaleString())}">▶ Review</button></td></tr>`;
      }).join('')+'</tbody></table>';
    }
    $('#previous').disabled=offset===0;$('#next').disabled=offset+100>=total;
    $('#page-label').textContent=total?`${offset+1}–${Math.min(offset+100,total)} of ${total}`:'';
  }catch(e){banner(e.message);}
}
function openCamera(id) {
  const c=id?state.cameras.find(c=>c.id===id):{id:'',name:'Foscam '+(state.cameras.length+1),host:'',port:88,username:'',stream:'videoMain',mode:'copy',control_port:88,control_https:false,audio:false},f=$('#camera-form');
  f.reset();f.querySelector('.form-error').textContent='';
  for(const key of ['id','name','host','port','username','stream','mode','control_port'])f.elements[key].value=c[key];
  f.elements.audio.checked=c.audio;f.elements.control_https.checked=c.control_https;f.elements.password.required=!c.has_password;
  f.elements.password.placeholder=c.has_password?'Leave blank to keep saved password':'Enter camera password';
  $('#camera-dialog').showModal();
}
function openSettings() {
  if(!state)return;
  const f=$('#settings-form');f.querySelector('.form-error').textContent='';
  for(const [key,value] of Object.entries(state.settings)) {
    if(key==='cleanup_enabled')f.elements[key].checked=value;else f.elements[key].value=value;
  }
  $('#settings-dialog').showModal();
}
document.addEventListener('click',async e=>{
  const close=e.target.closest('[data-close]');if(close){$('#'+close.dataset.close).close();return;}
  const probe=e.target.closest('[data-ptz]');
  if(probe){await ptzAction(probe.dataset.ptz,'/api/ptz-probe',{id:probe.dataset.ptz});return;}
  const edit=e.target.closest('[data-edit]');if(edit){openCamera(edit.dataset.edit);return;}
  const action=e.target.closest('[data-action]');
  if(action) {
    if(action.dataset.action==='setup'){openCamera(action.dataset.camera);return;}
    action.disabled=true;
    try {await api('/api/'+action.dataset.action,{id:action.dataset.camera});await refreshState();await refreshClips();}
    catch(e){banner(e.message);}finally{action.disabled=false;}
  }
  const play=e.target.closest('[data-play]');
  if(play) {
    $('#player-title').textContent=play.dataset.title;
    $('#player').src='/media/'+encodeURIComponent(play.dataset.play);
    $('#download').href=$('#player').src+'?download=1';
    $('#player-note').textContent='Playback is from your local hard drive.';
    $('#player-dialog').showModal();$('#player').play().catch(()=>{});
  }
});
$('#player-dialog').addEventListener('close',()=>{$('#player').pause();$('#player').removeAttribute('src');$('#player').load();});
$('#player').addEventListener('error',()=>{if($('#player').hasAttribute('src'))$('#player-note').textContent='This browser could not play the clip. Download it for VLC, or use Compatible mode for future recordings.';});
$('#camera-form').addEventListener('submit',async e=>{
  e.preventDefault();const f=e.target,b=f.querySelector('[type=submit]');b.disabled=true;
  const data=Object.fromEntries(new FormData(f));data.audio=f.elements.audio.checked;data.control_https=f.elements.control_https.checked;
  try{await api('/api/camera',data);f.elements.password.value='';$('#camera-dialog').close();await refreshState();}
  catch(e){f.querySelector('.form-error').textContent=e.message;}finally{b.disabled=false;}
});
$('#camera-dialog').addEventListener('close',()=>{$('#camera-form').elements.password.value='';});
$('#settings-form').addEventListener('submit',async e=>{
  e.preventDefault();const f=e.target,b=f.querySelector('[type=submit]');b.disabled=true;
  const data=Object.fromEntries(new FormData(f));data.cleanup_enabled=f.elements.cleanup_enabled.checked;
  try{await api('/api/settings',data);$('#settings-dialog').close();await refreshState();}
  catch(e){f.querySelector('.form-error').textContent=e.message;}finally{b.disabled=false;}
});
$('#add-camera').onclick=()=>openCamera('');
$('#discover-button').onclick=()=>{if(state?.discovery)$('#discovery-form').elements.subnet.value=state.discovery.subnet;renderDiscovery();$('#discovery-dialog').showModal();};
$('#settings-button').onclick=openSettings;$('#nav-settings').onclick=openSettings;
$('#nav-archive').onclick=()=>$('.archive-section').scrollIntoView({behavior:'smooth'});
$('#refresh').onclick=async()=>{await refreshState();await refreshClips();};
for(const el of ['#filter-camera','#filter-date'])$(el).onchange=()=>{offset=0;refreshClips();};
$('#clear-filters').onclick=()=>{$('#filter-camera').value='';$('#filter-date').value='';offset=0;refreshClips();};
$('#previous').onclick=()=>{offset=Math.max(0,offset-100);refreshClips();};
$('#next').onclick=()=>{offset+=100;refreshClips();};
(async()=>{await refreshState();await refreshClips();})();
setInterval(async()=>{if(document.hidden)return;await refreshState();await refreshClips();},10000);

function renderDiscovery(){
  const d=state?.discovery;if(!d)return;
  $('#discovery-summary').textContent=d.message;
  $('#discover-button').textContent=d.running?'⌕ Scanning…':'⌕ Discover cameras';
  $('#run-discovery').disabled=d.running;
  $('#discovery-progress').textContent=d.message;
  $('#discovery-results').innerHTML=(d.results||[]).map(r=>`<div class="discovery-result"><strong>${esc(r.host)}</strong><span>${esc(r.brand)} · ${r.confirmed?'stream confirmed':'candidate only'}</span><small>${esc((r.evidence||[]).join('; '))}</small></div>`).join('');
}
$('#discovery-form').addEventListener('submit',async e=>{
  e.preventDefault();$('#run-discovery').disabled=true;
  try{await api('/api/discover',{subnet:e.target.elements.subnet.value});await refreshState();}
  catch(error){$('#discovery-progress').textContent=error.message;$('#run-discovery').disabled=false;}
});
async function ptzAction(id,url,data){
  if(cameraActions.has(id))return;
  cameraActions.add(id);
  document.querySelectorAll(`[data-ptz="${id}"],[data-sweep="${id}"],[data-track="${id}"]`).forEach(el=>el.disabled=true);
  let errorMessage='';
  try{await api(url,data);}catch(error){errorMessage=error.message;}
  finally{cameraActions.delete(id);await refreshState();if(errorMessage)banner(errorMessage);}
}
document.addEventListener('change',async e=>{
  if(e.target.dataset.sweep){
    const id=e.target.dataset.sweep,cam=state.cameras.find(c=>c.id===id);
    await ptzAction(id,'/api/sweep',{id,enabled:e.target.checked,map:cam.sweep_map});
  }
  if(e.target.dataset.track){
    const id=e.target.dataset.track,cam=state.cameras.find(c=>c.id===id);
    await ptzAction(id,'/api/sweep',{id,enabled:cam.sweep_enabled,map:e.target.value});
  }
});
