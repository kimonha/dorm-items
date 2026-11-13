import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import vm from 'node:vm';

const source=await fs.readFile(new URL('../static/app.js',import.meta.url),'utf8');
const intervalCode=source.slice(source.indexOf('function selectionIntervals()'),source.indexOf('function toggleSlot('));
const bookingCode=intervalCode+source.slice(source.indexOf('let bookingPending=false;'),source.indexOf('function bookingBadge'));
assert.ok(bookingCode.includes('async function book()'));

async function scenario({switchView=false,reject=false,disjoint=false,serverSlots=false}={}){
  let resolveRequest,rejectRequest,calls=0,paints=0,refreshes=0,slotReloads=0,payload;
  const button={disabled:false,textContent:'선택한 시간 예약',setAttribute(){},removeAttribute(){}};
  const date={value:'2030-01-07',disabled:false};
  const confirmation={innerHTML:'',textContent:'',setAttribute(){}};
  const slotButtons=[{disabled:false},{disabled:false}];
  const elements={'#book':button,'#booking-date':date,'#slots':{querySelectorAll:()=>slotButtons},'#selection':confirmation,'#open-my-booking':{}};
  const scope={
    $:s=>elements[s],currentItem:{id:1,name:'청소기'},slotSequence:1,
    slots:[{start:'09:00',end:'09:30',available:true},{start:'09:30',end:'10:00',available:true},{start:'10:00',end:'10:30',available:true}],selected:disjoint?[0,2]:[0,1],
    api:(_,data)=>{payload=data;calls++;return new Promise((resolve,reject)=>{resolveRequest=resolve;rejectRequest=reject;});},
    toast(){},esc:s=>s,paintSlots(){paints++;},refreshRecords(){refreshes++;},
    loadSlots:async()=>{slotReloads++;},view(){}
  };
  vm.createContext(scope);vm.runInContext(bookingCode,scope);
  const request=scope.book();
  assert.equal(button.textContent,'예약을 저장하는 중…');
  assert.equal(button.disabled,true);
  assert.equal(date.disabled,true);
  assert.equal(paints,0,'Do not mark successful before the server confirms');
  await scope.book();assert.equal(calls,1,'Double clicks must not submit twice');
  if(switchView)scope.slotSequence++;
  if(reject)rejectRequest(new Error('이미 예약된 시간입니다.'));
  else resolveRequest({reservation:{id:12},...(serverSlots?{slots:[{start:'09:00',end:'09:30',available:false,remaining:2,quantity:3,reason:'내 예약'},{start:'09:30',end:'10:00',available:false,remaining:2,quantity:3,reason:'내 예약'}]}:{})});
  await request;
  assert.equal(button.textContent,'선택한 시간 예약');
  assert.equal(date.disabled,false);
  if(reject){assert.equal(paints,0);assert.equal(slotReloads,1);}
  else if(switchView){assert.equal(paints,0);assert.equal(refreshes,1);}
  else{
    assert.equal(slotReloads,0,'Successful booking should not wait on an extra GET');
    assert.equal(paints,1);
    assert.equal(scope.slots[0].available,false);
    assert.equal(scope.slots[disjoint?2:1].reason,'내 예약');
    assert.match(confirmation.innerHTML,/예약 완료 · #12/);
    if(disjoint){
      assert.equal(payload.intervals.length,2);
      assert.equal(payload.intervals[0].end,'09:30');
      assert.equal(payload.intervals[1].start,'10:00');
      assert.match(confirmation.innerHTML,/09:00–09:30 \/ 10:00–10:30/);
    }else assert.match(confirmation.innerHTML,/09:00–10:00/);
    if(serverSlots)assert.equal(scope.slots[0].remaining,2);
  }
}
await scenario();await scenario({switchView:true});await scenario({reject:true});await scenario({disjoint:true});await scenario({serverSlots:true});
console.log('Booking UI: pending feedback, duplicate prevention, immediate confirmation, stale view and failure handling passed.');
