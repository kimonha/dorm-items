import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import vm from 'node:vm';
const source=await fs.readFile(new URL('../static/app.js',import.meta.url),'utf8');
const code=source.slice(source.indexOf('let notificationTimer='),source.indexOf("api('/me').then"));
const elements=new Map();
const element=s=>{
  if(!elements.has(s))elements.set(s,{hidden:true,textContent:'',innerHTML:'',attributes:{},querySelectorAll:()=>[],setAttribute(k,v){this.attributes[k]=v;},removeAttribute(k){delete this.attributes[k];}});
  return elements.get(s);
};
const storage=new Map(),toasts=[],timers=[];
let nextResponse;
const scope={
  $:element,user:{id:10},localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},
  document:{addEventListener(){},hidden:false},window:{addEventListener(){}},
  setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length;},clearTimeout(){},
  api:async()=>{if(nextResponse instanceof Error)throw nextResponse;return nextResponse;},
  esc:s=>String(s),fmt:s=>s,toast:s=>toasts.push(s),returnDialog(){},refreshRecords(){},view(){}
};
vm.createContext(scope);vm.runInContext(code,scope);
const base={count:0,notifications:[],server_time:'2030-01-07T00:00:00Z',next_due_at:'2030-01-07T00:00:01Z'};
nextResponse=base;await scope.loadNotifications();
assert.equal(toasts.length,0);assert.equal(timers.at(-1).delay,1150,'Refresh just after nearest due time');
assert.equal(element('#notification-count').hidden,true);
const alert={id:'overdue-9',usage_id:9,item_name:'청소기',home_location:'2층 공용실',end_at:'2030-01-07T00:00:00Z',overdue_seconds:10,message:'청소기 반납이 필요합니다.'};
nextResponse={...base,count:1,notifications:[alert],next_due_at:null};
await scope.loadNotifications();
assert.equal(element('#notification-count').textContent,'1');
assert.equal(element('#overdue-panel').hidden,false);
assert.match(element('#overdue-content').innerHTML,/청소기/);
assert.match(element('#overdue-content').innerHTML,/반납 완료/);
assert.equal(toasts.length,1);
await scope.loadNotifications();assert.equal(toasts.length,1,'Polling must not repeat the same toast');
element('#close-notifications').onclick();await scope.loadNotifications();
assert.equal(element('#overdue-panel').hidden,true,'Keep panel collapsed until a new reminder');
nextResponse=new Error('offline');await scope.loadNotifications();
assert.equal(element('#notification-error').hidden,false);
assert.equal(element('#notification-count').textContent,'1','Failure must not erase known reminders');
nextResponse={...base,next_due_at:null};await scope.loadNotifications({force:true});
assert.equal(element('#notification-count').hidden,true);
assert.equal(element('#overdue-panel').hidden,true);
assert.equal(element('#notification-error').hidden,true);
// Ignore an in-flight result after logout, rather than displaying another user's reminders.
let resolveOld;
scope.api=()=>new Promise(resolve=>{resolveOld=resolve;});
const pending=scope.loadNotifications();scope.stopNotifications();scope.user=null;
resolveOld({...base,count:1,notifications:[alert]});await pending;
assert.equal(element('#notification-count').hidden,true);
assert.equal(element('#overdue-panel').hidden,true);
console.log('Notifications UI: due-time scheduling, banner, return action, duplicate suppression, collapse, error retry, return cleanup and logout race passed.');
