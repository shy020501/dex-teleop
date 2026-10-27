// A running server may still serve its cached pre-language HTML until restarted.
// Keep that page's replay controls usable while the new browser files are installed.
const replayLocale = window.B2DI18n || (() => {
 const t=(key,params={})=>String(key??'').replace(/\{(\w+)\}/g,(match,name)=>params[name]??match);
 const format=(target,render,attribute='textContent')=>{const element=typeof target==='string'?$(target):target;if(attribute==='textContent')element.textContent=render();else element.setAttribute(attribute,render());};
 return {t,message:value=>String(value??''),format,
  text:(target,key,params={},attribute)=>format(target,()=>t(key,params),attribute),
  bindMessage:(target,value,attribute)=>format(target,()=>String(value??''),attribute)};
})();
const replayText=replayLocale.t;
/* All frames are generated first. Both images decode before either is shown. */
let replayJob=null,replayTimer=null,replayPlaying=false,replayGeneration=0,replayFrame=0,replayDraw=0,replayPlayback=0;
function setupReplay(info){
 cleanupReplay();
 $('createReplay').disabled=!info.replay_available||debugMode;
 replayLocale.text('replayProgress',!info.replay_available?'Replay 불가: 이전 형식 · 업데이트 후 수집한 데이터에서 사용 가능':debugMode?'일반 실행 모드에서 사용 가능':'생성 중 조작·수집 일시 정지');
}
async function cleanupReplay(){
 ++replayGeneration;++replayDraw;++replayPlayback;replayPlaying=false;clearTimeout(replayTimer);
 const previous=replayJob;replayJob=null;
 $('details').classList.remove('replay-open');$('replayPanel').hidden=true;$('removeReplay').hidden=true;replayLocale.text('removeReplay','취소·삭제');
 replayLocale.text('replayPlay','재생');$('replayLeft').removeAttribute('src');$('replayRight').removeAttribute('src');
 if(previous){try{await api('/api/replay/remove',{id:previous.id});}catch(e){/* TTL handles lost connections. */}}
}
$('details').addEventListener('close',()=>{++detailsRequest;cleanupReplay();});
window.addEventListener('pagehide',()=>{if(replayJob)fetch('/api/replay/remove',{method:'POST',headers:{'Content-Type':'application/json','X-Dex-Teleop-Control':'1'},body:JSON.stringify({id:replayJob.id}),keepalive:true}).catch(()=>{});});
$('createReplay').onclick=async()=>{
 if(!selectedEpisode||replayJob)return;
 const generation=++replayGeneration;$('createReplay').disabled=true;replayLocale.text('replayProgress','요청 중…');
 try{
  const job=await api('/api/replay',{path:selectedEpisode});
  if(generation!==replayGeneration){await api('/api/replay/remove',{id:job.id});return;}
  replayJob=job;$('removeReplay').hidden=false;pollReplay(generation);
 }catch(e){$('createReplay').disabled=false;replayLocale.bindMessage('replayProgress',e.message);}
};
$('removeReplay').onclick=async()=>{await cleanupReplay();$('createReplay').disabled=false;replayLocale.text('replayProgress','임시 replay 삭제됨');};
async function pollReplay(generation){
 if(!replayJob||generation!==replayGeneration)return;
 try{
  const status=await api('/api/replay?id='+replayJob.id);
  if(generation!==replayGeneration)return;
  replayJob=status;replayLocale.format('replayProgress',()=>replayLocale.message(status.message)+' · '+Math.round(status.progress*100)+'%');
  replayLocale.bindMessage('replayProgress',status.detail||'','title');
  if(status.status==='ready'){
   if($('replayPanel').hidden){
    $('replayPanel').hidden=false;$('details').classList.add('replay-open');replayLocale.text('removeReplay','Replay 삭제');
    $('replayCamera').replaceChildren(...status.cameras.map(c=>{const o=document.createElement('option');o.value=c;replayLocale.text(o,({cam_task:'메인',cam_wrist_left:'왼쪽 손목',cam_wrist_right:'오른쪽 손목'})[c]||c);return o;}));
    if(status.cameras.includes('cam_task'))$('replayCamera').value='cam_task';
    $('replaySeek').max=status.frames-1;replayLocale.format('replayNote',()=>replayLocale.message(status.manifest.note)+' '+replayText('창을 닫으면 임시 영상이 삭제됩니다.'));await showReplayFrame(0);
   }
  }
  replayTimer=setTimeout(()=>pollReplay(generation),status.status==='ready'?15000:700);
 }catch(e){if(generation===replayGeneration){replayLocale.bindMessage('replayProgress',e.message);replayTimer=setTimeout(()=>pollReplay(generation),2000);}}
}
async function showReplayFrame(index){
 const job=replayJob;if(!job||job.status!=='ready')return;
 index=Math.min(job.frames-1,Math.max(0,index));const draw=++replayDraw;
 const camera=$('replayCamera').value,left=$('replayLeftMode').value;
 function imageFor(side){const img=new Image();img.src='/api/replay/image?'+new URLSearchParams({id:job.id,side,camera,frame:index});return img.decode().then(()=>img);}
 const [a,b]=await Promise.all([imageFor(left),imageFor('action')]);
 if(draw!==replayDraw||replayJob?.id!==job.id)return;
 a.id='replayLeft';replayLocale.text(a,'같은 step의 원본 또는 상태 재생',{},'alt');b.id='replayRight';replayLocale.text(b,'같은 step의 Action replay',{},'alt');
 $('replayLeft').replaceWith(a);$('replayRight').replaceWith(b);replayFrame=index;$('replaySeek').value=index;
 replayLocale.format('replayLeftLabel',()=>replayText($('replayLeftMode').value==='state'?'저장 상태로 다시 렌더링':'저장된 원본 RGB'));
 const step=job.steps[index],t=(step-job.steps[0])*job.physics_dt;
 $('replayStep').textContent=`Frame ${index+1} / ${job.frames} · Physics step ${step} · ${t.toFixed(3)} s`;
 const m=job.manifest.metrics[index];replayLocale.text('replayMetrics','저장 상태 ↔ Action replay | 관절 최대 오차 {joint}° · 물체 위치 {object} mm · 로봇 링크 {link} mm',{joint:(m.joint_max_rad*180/Math.PI).toFixed(2),object:(m.object_position_max_m*1000).toFixed(2),link:(m.link_position_max_m*1000).toFixed(2)});
}
function stopReplay(){++replayPlayback;replayPlaying=false;replayLocale.text('replayPlay','재생');}
async function stepReplay(index){stopReplay();try{await showReplayFrame(index);}catch(e){replayLocale.bindMessage('replayProgress',e.message);}}
$('replayPrev').onclick=()=>stepReplay(replayFrame-1);$('replayNext').onclick=()=>stepReplay(replayFrame+1);
$('replaySeek').oninput=()=>stepReplay(Number($('replaySeek').value));
$('replayCamera').onchange=() =>stepReplay(replayFrame);$('replayLeftMode').onchange=()=>stepReplay(replayFrame);
$('replayPlay').onclick=async()=>{
 if(replayPlaying){stopReplay();return;}if(!replayJob||replayJob.status!=='ready')return;
 const id=replayJob.id,playback=++replayPlayback;replayPlaying=true;replayLocale.text('replayPlay','일시 정지');
 try{
  if(replayFrame===replayJob.frames-1)await showReplayFrame(0);
  while(playback===replayPlayback&&replayPlaying&&replayJob?.id===id&&replayFrame<replayJob.frames-1){
   const current=replayFrame,delay=(replayJob.steps[current+1]-replayJob.steps[current])*replayJob.physics_dt*1000;
   await new Promise(resolve=>setTimeout(resolve,delay));if(playback!==replayPlayback||!replayPlaying||replayJob?.id!==id)break;
   await showReplayFrame(current+1);
  }
 }catch(e){replayLocale.bindMessage('replayProgress',e.message);}finally{if(playback===replayPlayback)stopReplay();}
};
