const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=s=>s?new Date(s).toLocaleString('ko-KR',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';
const day=d=>new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).format(d);
const today=()=>day(new Date(Date.now()+serverOffset));
const time=n=>`${String(Math.floor(n/60)).padStart(2,'0')}:${String(n%60).padStart(2,'0')}`;
const week=['월','화','수','목','금','토','일'];
const titles={items:'공용 물품',mine:'내 예약과 이용',admin:'물품·이용 관리',summary:'AI 이용 요약'};
const icons={'청소용품':'🧹','운반용품':'🛒','생활용품':'🧰','운동용품':'🏸'};
const itemPhotos={'CLEAN-001':'vacuum.jpg','IRON-001':'steamer.jpg','CART-001':'cart.jpg','UMBRELLA-001':'umbrella.jpg'};
function itemArtwork(item){
  const filename=itemPhotos[item.code];
  return filename?`<img class="item-photo" src="/static/images/${filename}" alt="${esc(item.name)}" width="600" height="400" loading="lazy" decoding="async">`:`<span class="emoji" aria-hidden="true">${icons[item.category]||'📦'}</span>`;
}

const actionNames={registered:'물품 등록',edited:'물품 정보 변경',reserved:'예약 완료',cancelled:'예약 취소',started:'사용 시작',returned:'반납 완료',admin_returned:'관리자 예외 반납'};
let user=null,csrf='',items=[],currentItem=null,detailSequence=0,slotSequence=0,searchSequence=0,slots=[],selected=[],activeView='items',serverOffset=0;
const empty=s=>`<div class="empty">${esc(s)}</div>`;
function toast(s,error=false){$('#toast').textContent=s;$('#toast').className=error?'error-toast':'';$('#toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('#toast').hidden=true,5000);}
async function api(path,data,method){let res;try{res=await fetch('/api'+path,{method:method||(data===undefined?'GET':'POST'),headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:data===undefined?undefined:JSON.stringify(data)});}catch{throw Error('서버 연결에 실패했습니다. 연결 상태를 확인하고 다시 시도해 주세요.');}let result;try{result=await res.json();}catch{throw Error('서버 응답을 읽을 수 없습니다. 다시 시도해 주세요.');}if(!res.ok){if(res.status===401&&path!='/login')loggedOut();throw Error(result.error||'요청에 실패했습니다.');}if(result.server_time)serverOffset=new Date(result.server_time).getTime()-Date.now();return result;}
function loggedOut(){stopNotifications();user=null;csrf='';$('#app-view').hidden=true;$('#login-view').hidden=false;document.querySelectorAll('dialog[open]').forEach(d=>d.close());}
function loggedIn(d){user=d.user;csrf=d.csrf_token;$('#app-view').hidden=false;$('#login-view').hidden=true;$('#user-name').textContent=user.username;$('#user-role').textContent=user.role==='admin'?'관리 담당자':'기숙사 거주 학생';document.querySelectorAll('.admin-only').forEach(e=>e.hidden=user.role!=='admin');view('items');startNotifications();}
$('#login-form').onsubmit=async e=>{e.preventDefault();const b=e.target.querySelector('button');b.disabled=true;$('#login-error').textContent='';try{loggedIn(await api('/login',Object.fromEntries(new FormData(e.target))));e.target.reset();}catch(e){$('#login-error').textContent=e.message;}finally{b.disabled=false;}};
$('#logout').onclick=async()=>{try{await api('/logout',{});loggedOut();}catch(e){toast(e.message,true);}};
for(const id of ['mine','admin','summary']){const form=$('#'+id+'-period');form.elements.from.value=day(new Date(Date.now()-29*86400000));form.elements.to.value=day(new Date(Date.now()+(id==='summary'?0:30)*86400000));}
function view(name){activeView=name;document.querySelectorAll('.main-content>section').forEach(e=>e.hidden=e.id!==name+'-view');document.querySelectorAll('nav [data-view]').forEach(e=>e.classList.toggle('active',e.dataset.view===name));$('#breadcrumb').textContent=titles[name];if(name==='items')loadItems();if(name==='mine'){loadActive();loadRecords('mine',1);}if(name==='admin')loadAdmin();}
document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>view(b.dataset.view));
function stateBadge(i){return `<span class="badge ${i.status==='available'?'available':i.is_overdue?'broken':'busy'}">${i.status==='available'?'사용 가능':i.is_overdue?'사용 중 · 반납 지연':'사용 중'}</span>`;}
function stockText(i){return `보유 ${i.quantity}개 · 현재 예약·사용 ${i.occupied_count??0}개 · 사용 가능 ${i.available_count??i.quantity}개`;}
function retry(target,message,fn){target.innerHTML=`<div class="empty"><p class="error">${esc(message)}</p><button class="secondary">다시 시도</button></div>`;target.querySelector('button').onclick=fn;}
async function loadItems(){const seq=++searchSequence;const target=$('#item-grid');target.innerHTML=empty('물품을 불러오는 중…');try{const d=await api('/items?q='+encodeURIComponent($('#search').value)+'&category='+encodeURIComponent($('#category').value));if(seq!==searchSequence)return;items=d.items;const previous=$('#category').value;$('#category').innerHTML='<option value="">모든 종류</option>'+d.categories.map(c=>`<option value="${esc(c)}">${esc(c)}</option>`).join('');$('#category').value=previous;$('#item-count').textContent=items.length;$('#stats').innerHTML=[['▦','검색된 물품',items.length],['✓','사용 가능',items.filter(i=>i.status==='available').length],['◷','사용 중',items.filter(i=>i.status==='in_use').length]].map(([icon,label,n])=>`<div class="stat"><div class="stat-icon">${icon}</div><span>${label}</span><strong>${n}<small> 개</small></strong></div>`).join('');target.innerHTML=items.length?items.map(i=>`<article class="item-card" role="button" tabindex="0" data-detail="${i.id}" aria-label="${esc(i.name)} 상세 정보와 예약 시간표 열기"><div class="item-art ${esc(i.category)}">${itemArtwork(i)}${stateBadge(i)}</div><div class="card-body"><span class="category-label">${esc(i.category)} · ${esc(i.code)}</span><h3>${esc(i.name)}</h3><p class="location">⌖ 지정 보관: ${esc(i.home_location)}</p><div class="stock-count">${stockText(i)}</div><div class="current-info">최근 반납 등록: ${fmt(i.last_returned_at)}<br>갱신: ${fmt(i.updated_at)}</div><div class="card-footer"><small>30분 단위 · 최대 2시간</small><span class="card-detail-link" aria-hidden="true">시간표 확인 ↗</span></div></div></article>`).join(''):empty('검색 결과가 없습니다. 다른 이름이나 종류로 검색해 보세요.');bindDetails(target);}catch(e){if(seq===searchSequence){$('#stats').innerHTML='';$('#item-count').textContent='';retry(target,e.message,loadItems);}}}
function bindDetails(target){
  target.querySelectorAll('[data-detail]').forEach(element=>{
    element.onclick=()=>detail(Number(element.dataset.detail));
    if(element.getAttribute('role')==='button')element.onkeydown=event=>{
      if(event.key==='Enter'||event.key===' '){event.preventDefault();element.click();}
    };
  });
}
$('#search-form').onsubmit=e=>{e.preventDefault();loadItems();};$('#category').onchange=loadItems;$('#refresh-items').onclick=loadItems;
$('#detail-dialog').addEventListener('close',()=>{detailSequence++;slotSequence++;});
async function detail(id){const seq=++detailSequence;$('#detail-content').innerHTML='<div class="modal-body">물품을 불러오는 중…</div>';if(!$('#detail-dialog').open)$('#detail-dialog').showModal();try{const d=await api('/items/'+id);if(seq!==detailSequence)return;const i=d.item;currentItem=i;$('#detail-content').innerHTML=`<div class="modal-header"><h2>${esc(i.name)}</h2><button class="close" aria-label="닫기">×</button></div><div class="modal-body"><div id="detail-stock-status">${stateBadge(i)}</div><p id="detail-stock-count" class="stock-count">${stockText(i)}</p><p class="preserve muted">${esc(i.description)}</p><div class="home-box"><strong>⌖ 지정 보관 장소: ${esc(i.home_location)}</strong>사용 후 이 장소에 반납하고 ‘반납 완료’를 등록해 주세요.<br>최근 반납 완료: ${fmt(i.last_returned_at)}<br><span class="meta-small">반납 여부는 이용자의 등록에 기반하며, 실제 위치를 자동 감지하지 않습니다.</span></div><div class="hours-list">${i.hours.map(h=>`${week[h.weekday]} ${time(h.open_min)}–${time(h.close_min)}`).join(' · ')}</div>${i.is_overdue?'<p class="notice">일부 물품의 반납이 지연되고 있습니다. 보유 수량이 모두 사용 중이면 다음 이용자의 사용 시작이 제한됩니다.</p>':''}<div class="tabs"><button class="active" data-tab="reserve">예약 시간표</button><button data-tab="history">${user.role==='admin'?'전체':'내'} 이용 기록</button></div><div id="detail-tab"></div></div>`;$('#detail-content .close').onclick=()=>$('#detail-dialog').close();document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{slotSequence++;document.querySelectorAll('[data-tab]').forEach(e=>e.classList.toggle('active',e===b));if(b.dataset.tab==='reserve')renderReserve();else renderHistory();});renderReserve();}catch(e){if(seq===detailSequence)retry($('#detail-content'),e.message,()=>detail(id));}}
function renderReserve(){selected=[];$('#detail-tab').innerHTML=`<label>이용 날짜<input id="booking-date" type="date" value="${today()}" min="${today()}" required></label><p class="hint">30분 단위, 최대 2시간까지 예약할 수 있습니다. 원하는 30분 구간을 각각 선택하세요. 다시 누르면 해당 구간만 해제됩니다. 떨어진 시간은 별도 예약으로 함께 저장됩니다. 수량이 모두 예약되면 해당 시간대는 마감됩니다.</p><div id="slots" class="slots"></div><p id="selection" class="hint">예약할 시간을 선택해 주세요.</p><button id="book" class="primary wide" disabled>선택한 시간 예약</button>`;$('#booking-date').onchange=loadSlots;$('#book').onclick=book;loadSlots();}
async function loadSlots(){const seq=++slotSequence;const id=currentItem.id,date=$('#booking-date').value;selected=[];$('#book').disabled=true;$('#slots').innerHTML=empty('시간표를 불러오는 중…');$('#selection').textContent='예약할 시간을 선택해 주세요.';try{const d=await api(`/items/${id}/availability?date=${date}`);if(seq!==slotSequence||!$('#booking-date')||currentItem.id!==id)return;slots=d.slots.filter(s=>s.reason!=='운영 시간 외');paintSlots();}catch(e){if(seq===slotSequence&&$('#slots'))retry($('#slots'),e.message,loadSlots);}}
function selectionIntervals(){
  const intervals=[];
  for(const n of [...selected].sort((a,b)=>a-b)){
    const last=intervals.at(-1);
    if(last&&last.end===slots[n].start)last.end=slots[n].end;
    else intervals.push({start:slots[n].start,end:slots[n].end});
  }
  return intervals;
}
function toggleSlot(n){
  if(bookingPending||!slots[n]?.available)return;
  if(selected.includes(n))selected=selected.filter(index=>index!==n);
  else{
    if(selected.length>=4){toast('선택한 시간의 합계는 최대 2시간입니다.',true);return;}
    selected=[...selected,n].sort((a,b)=>a-b);
  }
  paintSlots();
}
function paintSlots(){
  $('#slots').innerHTML=slots.length?slots.map((s,n)=>{
    const label=s.reason||'예약 가능';
    return `<button class="slot ${s.available?'':'unavailable'} ${selected.includes(n)?'selected':''}" data-slot="${n}" ${s.available?'':'disabled'} aria-pressed="${selected.includes(n)}" aria-label="${s.start}부터 ${s.end}까지 ${label}">${s.start}<small>${label}</small></button>`;
  }).join(''):empty('이 날짜는 운영하지 않습니다. 다른 날짜를 선택해 주세요.');
  document.querySelectorAll('[data-slot]').forEach(b=>b.onclick=()=>toggleSlot(Number(b.dataset.slot)));
  const intervals=selectionIntervals();
  $('#selection').textContent=selected.length?`${$('#booking-date').value} · ${intervals.map(s=>s.start+'–'+s.end).join(' / ')} (합계 ${selected.length*30}분)`:'예약할 시간을 선택해 주세요.';
  $('#book').disabled=!selected.length||bookingPending;
}
let bookingPending=false;
async function book(){
  if(bookingPending||!selected.length)return;
  const button=$('#book'),dateInput=$('#booking-date');
  const context={item:currentItem,date:dateInput.value,sequence:slotSequence};
  const chosen=[...selected];
  const intervals=selectionIntervals();
  const payload={item_id:context.item.id,date:context.date,intervals};
  bookingPending=true;
  button.disabled=true;
  button.textContent='예약을 저장하는 중…';
  button.setAttribute('aria-busy','true');
  dateInput.disabled=true;
  $('#slots').querySelectorAll('button').forEach(b=>b.disabled=true);
  try{
    const result=await api('/reservations',payload);
    toast('예약이 완료되었습니다.');
    const serverTime=Date.parse(result.server_time);
    const starts=(result.reservations||[result.reservation]).map(r=>Date.parse(r.start_at)-serverTime);
    const startsSoon=starts.some(ms=>ms<=300000);
    if(startsSoon)loadNotifications({force:true});
    else if(starts.some(Number.isFinite)&&user&&!notificationsBusy){
      clearTimeout(notificationTimer);
      notificationTimer=setTimeout(()=>loadNotifications(),Math.min(30000,Math.min(...starts)-300000+150));
    }
    // Confirmed by the server: update this timetable directly, without a second request.
    if(context.sequence===slotSequence&&$('#book')===button&&currentItem.id===context.item.id){
      if(result.slots)slots=result.slots.filter(s=>s.reason!=='운영 시간 외');
      else for(const n of chosen)slots[n]={...slots[n],available:false,reason:'내 예약'};
      selected=[];
      bookingPending=false;
      paintSlots();
      $('#selection').innerHTML=`<strong>✓ 예약 완료 · #${(result.reservations||[result.reservation]).map(r=>r.id).join(', #')}</strong><br>${esc(context.item.name)} · ${esc(context.date)} ${intervals.map(s=>esc(s.start)+'–'+esc(s.end)).join(' / ')}<br><button id="open-my-booking" class="secondary">내 예약 확인</button>`;
      if(startsSoon){
        $('#selection').insertAdjacentHTML('beforeend','<p><strong>곧 예약 시간이에요. 사용하시겠어요?</strong><br>예약 시간이 되면 알림의 ‘사용 시작’을 눌러 주세요.</p>');
      }
      $('#selection').setAttribute('role','status');
      if(result.item){
        currentItem=result.item;
        const status=$('#detail-stock-status'),stock=$('#detail-stock-count');
        if(status)status.innerHTML=stateBadge(result.item);
        if(stock)stock.textContent=stockText(result.item);
        if(activeView==='items')loadItems();
      }
      $('#open-my-booking').onclick=()=>{
        const form=$('#mine-period');
        if(form.elements.from.value>context.date)form.elements.from.value=context.date;
        if(form.elements.to.value<context.date)form.elements.to.value=context.date;
        $('#detail-dialog').close();view('mine');
      };
    }else{
      // The request completed after the user switched tabs; refresh any visible record list.
      refreshRecords();
    }
  }catch(error){
    toast(error.message,true);
    if(context.sequence===slotSequence&&$('#book')===button){
      $('#selection').textContent=error.message;
      await loadSlots();
    }
  }finally{
    bookingPending=false;
    if($('#book')===button){
      button.textContent='선택한 시간 예약';
      button.removeAttribute('aria-busy');
      dateInput.disabled=false;
      button.disabled=!selected.length;
    }
  }
}
function bookingBadge(r){if(r.status==='cancelled')return '<span class="badge busy">예약 취소</span>';if(r.usage_status==='returned')return `<span class="badge available">반납 완료${r.was_late?' · 지연 등록':''}</span>`;if(r.usage_id)return `<span class="badge ${r.is_overdue?'broken':'busy'}">사용 중${r.is_overdue?' · 반납 지연':''}</span>`;return '<span class="badge available">예약 완료</span>';}
function recordHTML(r){return `<article class="record"><div class="record-heading"><h3>${esc(r.item_name)} <small class="muted">#${r.id}</small></h3>${bookingBadge(r)}</div><p>예약: ${fmt(r.start_at)} – ${fmt(r.end_at)}<br>지정 보관: ${esc(r.home_location)}${r.username?` · 이용자: ${esc(r.username)}`:''}<br>사용 시작: ${fmt(r.started_at)}<br>반납 완료: ${fmt(r.returned_at)}${r.cancelled_at?`<br>예약 취소: ${fmt(r.cancelled_at)}`:''}</p><div class="actions">${r.can_start?`<button class="primary" data-start="${r.id}">사용 시작</button>`:''}${r.can_return?`<button class="primary" data-return="${r.usage_id}" data-mine="${r.is_mine}" data-home="${esc(r.home_location)}">${r.is_mine?'반납 완료':'관리자 예외 반납'}</button>`:''}${r.can_cancel?`<button class="secondary" data-cancel="${r.id}">예약 취소</button>`:''}<button class="secondary" data-detail="${r.item_id}">물품 보기</button></div>${r.status==='confirmed'&&!r.usage_id&&!r.can_start?'<p class="meta-small">사용 시작은 예약 시간 안에서만 가능합니다. 시작 시점에 새로 조회해 주세요.</p>':''}</article>`;}
function pager(d){return `<div class="pager"><button data-page="${d.page-1}" ${d.page===1?'disabled':''}>이전</button><span>${d.page} / ${Math.max(1,Math.ceil(d.total/d.page_size))} · ${d.total}건</span><button data-page="${d.page+1}" ${d.page*d.page_size>=d.total?'disabled':''}>다음</button></div>`;}
function range(form){return Object.fromEntries(new FormData(form));}
async function refreshRecords(){if(activeView==='mine'){loadActive();loadRecords('mine',1);}if(activeView==='admin'){loadRecords('admin',1);loadAudit(1);}if(activeView==='summary')loadEvidence(1);if(activeView==='items')loadItems();}
function bindRecords(target,reload){bindDetails(target);target.querySelectorAll('[data-start]').forEach(b=>b.onclick=async()=>{b.disabled=true;try{await api(`/reservations/${b.dataset.start}/start`,{});toast('사용을 시작했습니다. 사용 후 지정 장소에 반납해 주세요.');reload();loadActive();loadNotifications({force:true});}catch(e){toast(e.message,true);b.disabled=false;}});target.querySelectorAll('[data-cancel]').forEach(b=>b.onclick=async()=>{if(!confirm('이 예약을 취소하시겠어요?'))return;b.disabled=true;try{await api(`/reservations/${b.dataset.cancel}/cancel`,{});toast('예약을 취소했습니다.');reload();loadNotifications({force:true});}catch(e){toast(e.message,true);b.disabled=false;}});target.querySelectorAll('[data-return]').forEach(b=>b.onclick=()=>returnDialog(Number(b.dataset.return),b.dataset.mine==='true',b.dataset.home,reload));}
function returnDialog(id,mine,home,reload){$('#return-content').innerHTML=`<div class="modal-header"><h2>${mine?'반납 완료':'관리자 예외 반납'}</h2><button class="close" aria-label="닫기">×</button></div><form id="return-form" class="modal-body"><div class="home-box"><strong>⌖ 지정 보관 장소: ${esc(home)}</strong>실제로 지정된 장소에 반납한 후 완료를 등록해 주세요.</div><label class="confirm-label"><input type="checkbox" required>물품이 지정 보관 장소에 반납되었음을 확인했습니다.</label>${mine?'':'<label>예외 반납 사유<textarea name="reason" rows="3" maxlength="1000" required></textarea></label>'}<p class="meta-small">반납 완료 시각은 서버가 현재 시각으로 기록합니다. 위치를 입력하거나 변경하지 않습니다.</p><p id="return-error" class="error" role="alert"></p><button class="primary wide">반납 완료</button></form>`;$('#return-content .close').onclick=()=>$('#return-dialog').close();$('#return-dialog').showModal();$('#return-form').onsubmit=async e=>{e.preventDefault();const b=e.target.querySelector('button');b.disabled=true;try{await api(`/usages/${id}/return`,Object.fromEntries(new FormData(e.target)));$('#return-dialog').close();toast('반납이 완료되었습니다.');reload();loadActive();loadNotifications({force:true});}catch(e){$('#return-error').textContent=e.message;b.disabled=false;}};}
async function loadActive(){const target=$('#active-list');try{const d=await api('/my-active');target.innerHTML=d.records.length?d.records.map(recordHTML).join(''):empty('현재 사용 중인 물품이 없습니다.');bindRecords(target,loadActive);}catch(e){retry(target,e.message,loadActive);}}
async function loadRecords(mode,page){const target=$(mode==='mine'?'#mine-list':'#admin-records'),query=new URLSearchParams({...range($('#'+mode+'-period')),page,...(mode==='admin'?{all:1}:{})});target.innerHTML=empty('기록을 불러오는 중…');try{const d=await api('/reservations?'+query);target.innerHTML=(d.records.length?d.records.map(recordHTML).join(''):empty('이 기간과 겹치는 예약·이용 기록이 없습니다.'))+pager(d);target.querySelectorAll('[data-page]').forEach(b=>b.onclick=()=>loadRecords(mode,Number(b.dataset.page)));bindRecords(target,()=>loadRecords(mode,page));}catch(e){retry(target,e.message,()=>loadRecords(mode,page));}}
$('#mine-period').onsubmit=e=>{e.preventDefault();loadActive();loadRecords('mine',1);};$('#admin-period').onsubmit=e=>{e.preventDefault();loadRecords('admin',1);loadAudit(1);};
function renderHistory(){$('#detail-tab').innerHTML=`<p class="hint">${user.role==='admin'?'해당 물품의 전체 예약·이용 기록입니다.':'본인의 기록만 표시합니다. 다른 학생의 개인정보는 공개하지 않습니다.'}</p><form id="history-period" class="period"><label>시작일<input name="from" type="date" value="${day(new Date(Date.now()-29*86400000))}" required></label><label>종료일<input name="to" type="date" value="${day(new Date(Date.now()+30*86400000))}" required></label><button class="secondary">조회</button></form><div id="history-records"></div>`;$('#history-period').onsubmit=e=>{e.preventDefault();loadHistory(1);};loadHistory(1);}
async function loadHistory(page){const id=currentItem.id,target=$('#history-records');if(!target)return;try{const d=await api(`/items/${id}/history?`+new URLSearchParams({...range($('#history-period')),page}));if(!$('#history-records')||currentItem.id!==id)return;target.innerHTML=(d.records.length?d.records.map(recordHTML).join(''):empty('조회 기간에 이용 기록이 없습니다.'))+pager(d);target.querySelectorAll('[data-page]').forEach(b=>b.onclick=()=>loadHistory(Number(b.dataset.page)));bindRecords(target,()=>loadHistory(page));}catch(e){retry(target,e.message,()=>loadHistory(page));}}
async function loadAdmin(){try{const d=await api('/items');$('#admin-items').innerHTML=d.items.map(i=>`<div class="list-row"><strong>${esc(i.name)}</strong><span class="muted">${esc(i.code)} · ${esc(i.home_location)} · 보유 ${i.quantity}개</span><button class="secondary" data-edit="${i.id}">정보·운영 시간 수정</button></div>`).join('');$('#admin-items').querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>editItem(d.items.find(i=>i.id===Number(b.dataset.edit))));}catch(e){retry($('#admin-items'),e.message,loadAdmin);}loadRecords('admin',1);loadAudit(1);}
$('#add-item').onclick=()=>editItem();
function editItem(item){const i=item||{code:'',name:'',category:'',description:'',home_location:'',quantity:1,hours:week.map((_,weekday)=>({weekday,open_min:420,close_min:1380}))};$('#edit-content').innerHTML=`<div class="modal-header"><h2>${item?'물품 정보·운영 시간 수정':'공용 물품 등록'}</h2><button class="close" aria-label="닫기">×</button></div><form id="item-form" class="modal-body"><div class="form-grid">${[['code','물품 코드',40],['name','물품 이름',100],['category','종류',60],['home_location','지정 보관 장소',150]].map(([key,label,max])=>`<label>${label}<input name="${key}" value="${esc(i[key])}" maxlength="${max}" required></label>`).join('')}</div><label>보유 수량<input name="quantity" type="number" min="1" max="1000" step="1" value="${i.quantity??1}" required></label><label>물품 설명<textarea name="description" rows="3" maxlength="2000" required>${esc(i.description)}</textarea></label><h3>운영 요일·시간</h3><p class="hint">운영하는 요일을 선택하세요. 30분 단위, 같은 날 안에서 설정합니다. 종료 시각 24:00을 사용할 수 있습니다. 기존 예약과 충돌하는 변경은 거절됩니다.</p>${week.map((label,weekday)=>{const h=i.hours.find(h=>h.weekday===weekday);return `<div class="hours-row"><label><input name="day-${weekday}" type="checkbox" ${h?'checked':''}>${label}</label><input name="open-${weekday}" type="text" inputmode="numeric" value="${time(h?.open_min??420)}" aria-label="${label} 운영 시작"><span class="hours-colon">–</span><input name="close-${weekday}" type="text" inputmode="numeric" value="${time(h?.close_min??1380)}" aria-label="${label} 운영 종료"></div>`;}).join('')}<p id="edit-error" class="error" role="alert"></p><button class="primary wide">저장</button></form>`;$('#edit-content .close').onclick=()=>$('#edit-dialog').close();$('#edit-dialog').showModal();$('#item-form').onsubmit=async e=>{e.preventDefault();const fd=new FormData(e.target),data={};for(const key of ['code','name','category','description','home_location'])data[key]=fd.get(key);data.quantity=Number(fd.get('quantity'));data.hours=week.flatMap((_,weekday)=>fd.has('day-'+weekday)?[{weekday,open:fd.get('open-'+weekday),close:fd.get('close-'+weekday)}]:[]);if(item)data.version=item.version;const b=e.target.querySelector('button');b.disabled=true;try{await api('/items'+(item?'/'+item.id:''),data,item?'PUT':'POST');$('#edit-dialog').close();toast('물품 정보와 운영 시간을 저장했습니다.');loadAdmin();}catch(e){$('#edit-error').textContent=e.message;b.disabled=false;}};}
async function loadAudit(page){const target=$('#audit-records');try{const d=await api('/events?'+new URLSearchParams({...range($('#admin-period')),page}));target.innerHTML=(d.records.length?d.records.map(r=>`<div class="log-row"><strong>#${r.id} ${esc(actionNames[r.action])}</strong> · ${esc(r.name)} · ${esc(r.username)}<p>${esc(r.note||'')}<br>${fmt(r.created_at)}${r.reservation_id?' · 예약 #'+r.reservation_id:''}${r.usage_id?' · 이용 #'+r.usage_id:''}</p>${r.action==='edited'?`<p class="preserve muted">${esc(r.detail)}</p>`:''}</div>`).join(''):empty('해당 기간에 처리 이력이 없습니다.'))+pager(d);target.querySelectorAll('[data-page]').forEach(b=>b.onclick=()=>loadAudit(Number(b.dataset.page)));}catch(e){retry(target,e.message,()=>loadAudit(page));}}
let summaryRange=null;
$('#summary-period').onsubmit=async e=>{e.preventDefault();const b=e.target.querySelector('button');b.disabled=true;summaryRange=range(e.target);$('#summary-content').innerHTML=empty('이용 기록을 집계하고 있어요…');$('#summary-evidence').innerHTML='';try{const d=await api('/summary?'+new URLSearchParams(summaryRange),{});$('#summary-content').innerHTML=`<div class="summary-box"><span class="badge ${d.status==='ok'?'available':'busy'}">${d.status==='ok'?'✧ AI 요약':d.status==='empty'?'기록 없음':'AI 요약 이용 불가'}</span><h2>우리 기숙사 이용 인사이트</h2><p class="preserve">${esc(d.summary||d.message)}</p><p class="meta-small">집계 시각: ${fmt(d.as_of)} · 예약 ${d.record_count}건</p><p class="hint">${esc(d.basis)}</p></div>`+(d.items.length?`<div class="summary-box"><h2>물품별 근거 집계</h2><div class="facts-wrap"><table class="facts"><thead><tr><th>물품</th><th>예약</th><th>취소</th><th>사용</th><th>반납</th><th>미반납 지연</th><th>지연 반납 등록</th><th>등록 기준 이용 분</th></tr></thead><tbody>${d.items.map(i=>`<tr><td>#${i.item_id} ${esc(i.name)}</td><td>${i.reservation_count}</td><td>${i.cancelled_count}</td><td>${i.use_count}</td><td>${i.returned_count}</td><td>${i.overdue_count}</td><td>${i.late_return_count}</td><td>${i.registered_use_minutes}</td></tr>`).join('')}</tbody></table></div><h3>시간대별 예약된 이용 시간</h3><p>${Object.entries(d.busy_hours_minutes).sort(([a],[b])=>a.localeCompare(b)).map(([h,n])=>`${esc(h)} ${n}분`).join(' · ')||'예약 시간 집계 없음'}</p><p class="hint">AI에는 물품 번호와 숫자 집계만 전달합니다. 예약·반납 상태를 자동으로 변경하지 않습니다.</p></div>`:'');await loadEvidence(1);}catch(e){retry($('#summary-content'),e.message,()=>$('#summary-period').requestSubmit());await loadEvidence(1);}finally{b.disabled=false;}};
async function loadEvidence(page){if(!summaryRange)return;const target=$('#summary-evidence');try{const d=await api('/reservations?'+new URLSearchParams({...summaryRange,all:1,page}));target.innerHTML='<h2>원본 예약·이용 기록</h2>'+(d.records.length?d.records.map(recordHTML).join(''):empty('해당 기간에 원본 기록이 없습니다.'))+pager(d);target.querySelectorAll('[data-page]').forEach(b=>b.onclick=()=>loadEvidence(Number(b.dataset.page)));bindRecords(target,()=>loadEvidence(page));}catch(e){retry(target,e.message,()=>loadEvidence(page));}}
let notificationTimer=null,notificationGeneration=0,notificationsBusy=false,currentOverdue=[];
const notifiedUsages=new Set();
function notificationPreference(){try{return localStorage.getItem('dorm-browser-notifications:'+user.id)==='on';}catch{return false;}}
function browserNotificationsEnabled(){return user&&typeof Notification!=='undefined'&&Notification.permission==='granted'&&notificationPreference();}
function updateBrowserNotificationButton(){
  const button=$('#browser-notifications');
  button.hidden=typeof Notification==='undefined';
  if(!button.hidden)button.textContent=browserNotificationsEnabled()?'브라우저 알림 끄기':'브라우저 알림 켜기';
}
function stopNotifications(){
  notificationGeneration++;clearTimeout(notificationTimer);notificationTimer=null;notificationsBusy=false;currentOverdue=[];notifiedUsages.clear();
  $('#overdue-panel').hidden=true;$('#overdue-content').innerHTML='';$('#notification-error').hidden=true;$('#notification-count').hidden=true;$('#notification-count').textContent='0';
  $('#notifications-toggle').setAttribute('aria-expanded','false');$('#notifications-toggle').setAttribute('aria-label','알림');$('#notification-live').textContent='';
}
function startNotifications(){stopNotifications();updateBrowserNotificationButton();loadNotifications();}
function alreadyNotified(id){
  const key=user.id+':'+id;
  if(notifiedUsages.has(key))return true;
  try{if(localStorage.getItem('dorm-overdue-seen:'+key))return true;}catch{}
  return false;
}
function markNotified(id){
  const key=user.id+':'+id;notifiedUsages.add(key);
  try{localStorage.setItem('dorm-overdue-seen:'+key,'1');}catch{}
}
function showBrowserNotification(alert){
  if(!browserNotificationsEnabled())return;
  try{
    const notice=new Notification(alert.type==='reservation'?'공용 물품 예약 알림':'공용 물품 반납 알림',{body:alert.message,tag:'dorm-'+alert.id});
    notice.onclick=()=>{window.focus();view('mine');notice.close();};
  }catch{/* Unsupported browser notifications must not interrupt in-app reminders. */}
}
function overdueLabel(seconds){return seconds<60?'1분 미만':Math.floor(seconds/60)+'분';}
function renderNotifications(data){
  $('#notification-error').hidden=true;
  const reminders=data.reservation_reminders||[];
  const alerts=[...data.notifications,...reminders];
  const count=alerts.length;
  const wasEmpty=currentOverdue.length===0;
  const fresh=alerts.filter(alert=>!alreadyNotified(alert.id));
  const oldCount=currentOverdue.length;
  currentOverdue=alerts;
  $('#notification-count').textContent=String(count);$('#notification-count').hidden=count===0;
  $('#notifications-toggle').setAttribute('aria-label',count?`알림 ${count}건`:'알림');
  $('#notifications-toggle').removeAttribute('title');
  $('#overdue-content').innerHTML=`<div class="overdue-heading"><h2>${count?'알림 '+count+'건':'새로운 알림이 없습니다'}</h2><button id="close-notifications" class="text-button" aria-label="알림 접기">접기</button></div>`+
    (data.notifications.length?'<p class="muted">예약 시간이 지났습니다. 지정 장소에 반납한 뒤 반납 완료를 눌러 주세요.</p>':'')+
    data.notifications.map(alert=>`<article class="overdue-item"><div><strong>${esc(alert.item_name)}</strong><p>예약 종료: ${fmt(alert.end_at)} · ${overdueLabel(alert.overdue_seconds)} 지연<br>지정 보관: ${esc(alert.home_location)}</p></div><button class="primary" data-overdue-return="${alert.usage_id}">반납 완료</button></article>`).join('')+
    reminders.map(alert=>`<article class="overdue-item"><div><strong>${esc(alert.item_name)}</strong><p>${esc(alert.message)}<br>예약 시작: ${fmt(alert.start_at)}</p></div>${alert.ready?`<button class="primary" data-reminder-start="${alert.reservation_id}">사용 시작</button>`:''}</article>`).join('');
  $('#overdue-content').querySelectorAll('[data-reminder-start]').forEach(button=>button.onclick=async()=>{
    button.disabled=true;
    try{
      await api(`/reservations/${button.dataset.reminderStart}/start`,{});
      toast('사용을 시작했습니다.');
      refreshRecords();
      loadNotifications({force:true});
    }catch(error){toast(error.message,true);button.disabled=false;}
  });
  $('#close-notifications').onclick=()=>{$('#overdue-panel').hidden=true;$('#notifications-toggle').setAttribute('aria-expanded','false');};
  $('#overdue-content').querySelectorAll('[data-overdue-return]').forEach(button=>button.onclick=()=>{
    const alert=currentOverdue.find(a=>a.usage_id===Number(button.dataset.overdueReturn));
    if(alert)returnDialog(alert.usage_id,true,alert.home_location,()=>{refreshRecords();});
  });
  if(count&&(wasEmpty||fresh.length))$('#overdue-panel').hidden=false;
  if(!count&&oldCount)$('#overdue-panel').hidden=true;
  $('#notifications-toggle').setAttribute('aria-expanded',String(!$('#overdue-panel').hidden));
  if(fresh.length){
    for(const alert of fresh){showBrowserNotification(alert);markNotified(alert.id);}
    const message=fresh.length===1?fresh[0].message:`새 알림이 ${fresh.length}건 있습니다. 알림에서 확인해 주세요.`;
    toast(message,true);$('#notification-live').textContent=message;
  }else if(oldCount!==count)$('#notification-live').textContent=`알림 ${count}건`;
}
async function loadNotifications({force=false}={}){
  if(!user)return;
  if(force){notificationGeneration++;notificationsBusy=false;}
  if(notificationsBusy)return;
  clearTimeout(notificationTimer);notificationsBusy=true;
  const generation=notificationGeneration,userId=user.id;
  let delay=30000;
  try{
    const data=await api('/notifications');
    if(generation!==notificationGeneration||!user||user.id!==userId)return;
    renderNotifications(data);
    // Poll normally, but schedule the next request just after the nearest reservation deadline.
    if(data.next_due_at)delay=Math.min(delay,Math.max(150,new Date(data.next_due_at)-new Date(data.server_time)+150));
  }catch(error){
    if(generation===notificationGeneration&&user){
      $('#notifications-toggle').setAttribute('title','알림 업데이트에 실패했습니다. 눌러서 다시 확인해 주세요.');
      $('#notification-error').hidden=false;$('#notification-error').textContent='알림을 확인하지 못했습니다. 상단 알림 버튼을 눌러 다시 확인해 주세요.';
    }
  }finally{
    if(generation===notificationGeneration&&user?.id===userId){
      notificationsBusy=false;notificationTimer=setTimeout(()=>loadNotifications(),delay);
    }
  }
}
$('#notifications-toggle').onclick=()=>{
  const panel=$('#overdue-panel');panel.hidden=!panel.hidden;
  $('#notifications-toggle').setAttribute('aria-expanded',String(!panel.hidden));
  if(!panel.hidden)loadNotifications({force:true});
};
$('#browser-notifications').onclick=async()=>{
  if(!user||typeof Notification==='undefined')return;
  if(browserNotificationsEnabled()){
    try{localStorage.setItem('dorm-browser-notifications:'+user.id,'off');}catch{}
    updateBrowserNotificationButton();toast('브라우저 알림을 껐습니다.');return;
  }
  try{
    const permission=await Notification.requestPermission();
    if(permission==='granted'){
      localStorage.setItem('dorm-browser-notifications:'+user.id,'on');
      toast('브라우저 알림을 켰습니다. 서비스 페이지가 열려 있을 때 반납을 알려드립니다.');
      currentOverdue.forEach(showBrowserNotification);
    }else toast('브라우저 알림이 허용되지 않았습니다. 서비스 안의 알림에서 확인할 수 있습니다.');
  }catch{toast('이 브라우저에서는 기기 알림을 사용할 수 없습니다. 서비스 안의 알림에서 확인해 주세요.');}
  updateBrowserNotificationButton();
};
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&user)loadNotifications({force:true});});
window.addEventListener('focus',()=>{if(user)loadNotifications();});

api('/me').then(loggedIn).catch(loggedOut);
