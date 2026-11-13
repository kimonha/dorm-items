"""Requirement acceptance tests. Uses actual SQLite transactions; no HTTP port needed.
Set TEST_DATABASE_URL to run the same suite on a dedicated empty PostgreSQL test DB.
Never point TEST_DATABASE_URL to a production database.
"""
import concurrent.futures
import datetime as dt
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.now=dt.datetime(2030,1,7,8,0,tzinfo=app.KST)
        self.config={'LOCAL_DB':str(Path(self.temp.name)/'test.sqlite3'),'DATABASE_URL':'','TESTING':True,'CLOCK':lambda:self.now,'GEMINI_API_KEY':''}
        if os.environ.get('TEST_DATABASE_URL'):
            self.config.update(LOCAL_DB='',DATABASE_URL=os.environ['TEST_DATABASE_URL'])
        self.web=app.create_app(self.config)
        with patch.dict(os.environ,{'DEMO_STUDENT_PASSWORD':'student123!','DEMO_ADMIN_PASSWORD':'admin123!'}):
            with app.app_context(self.web):
                app.initialize(seed=False)
                with app.connect() as db:
                    for table in ['booking_catalog_updates','booking_retired_items','booking_events','booking_usages','booking_reservations','booking_hours','booking_items','booking_sessions','booking_users']:
                        db.execute('DELETE FROM '+table)
                app.initialize(seed=True)
                # Original acceptance scenarios exercise a one-unit item; capacity has its own tests.
                with app.connect(write=True) as db:
                    db.execute('UPDATE booking_items SET quantity=1')
        self.s,self.sh=self.login('student')
        self.other,self.oh=self.login('student2')
        self.admin,self.ah=self.login('admin')
        self.item=self.s.get('/api/items?q=청소기').json()['items'][0]['id']

    def tearDown(self):
        self.temp.cleanup()

    def login(self,name):
        client=TestClient(self.web, raise_server_exceptions=False)
        r=client.post('/api/login',json={'username':name,'password':'admin123!' if name=='admin' else 'student123!'})
        self.assertEqual(r.status_code,200,r.json())
        return client,{'X-CSRF-Token':r.json()['csrf_token']}

    def reserve(self,start='09:00',end='10:00',client=None,headers=None,expected=201,date='2030-01-07'):
        r=(client or self.s).post('/api/reservations',json={'item_id':self.item,'date':date,'start':start,'end':end},headers=headers or self.sh)
        self.assertEqual(r.status_code,expected,r.json())
        return r.json().get('reservation')

    def start(self,booking,client=None,headers=None,expected=201):
        r=(client or self.s).post(f"/api/reservations/{booking['id']}/start",json={},headers=headers or self.sh)
        self.assertEqual(r.status_code,expected,r.json())
        return r.json().get('usage')

    def returned(self,usage,client=None,headers=None,data=None,expected=200):
        r=(client or self.s).post(f"/api/usages/{usage['id']}/return",json=data or {},headers=headers or self.sh)
        self.assertEqual(r.status_code,expected,r.json())
        return r.json().get('usage')

    def count(self,table):
        with app.app_context(self.web):
            with app.connect() as db:
                return db.execute('SELECT count(*) AS n FROM '+table).fetchone()['n']

    def records(self,client=None,all_users=False):
        return (client or self.s).get('/api/reservations?from=2030-01-01&to=2030-01-31'+('&all=1' if all_users else '')).json()

    def test_T01_search_location_and_status(self):
        r=self.s.get('/api/items?q=청소기')
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()['items'][0]['home_location'],'2층 공용실')
        self.assertEqual(r.json()['items'][0]['status'],'available')
        self.assertEqual(len(self.s.get('/api/items?q=청소용품').json()['items']),1)

    def test_T02_no_search_results(self):
        self.assertEqual(self.s.get('/api/items?q=없는물품').json()['items'],[])
        self.assertEqual(self.s.get('/api/items/99999').status_code,404)

    def test_T03_valid_booking_and_not_in_use(self):
        self.reserve()
        self.assertEqual(self.count('booking_reservations'),1)
        self.assertEqual(self.s.get(f'/api/items/{self.item}').json()['item']['status'],'available')

    def test_T04_overlap_rejected_adjacent_allowed(self):
        self.reserve()
        self.reserve('09:30','10:30',expected=409)
        self.reserve('10:00','11:00')
        self.assertEqual(self.count('booking_reservations'),2)

    def test_T05_concurrent_booking_one_winner(self):
        def book(n):
            client,headers=self.login('student' if n%2==0 else 'student2')
            return client.post('/api/reservations',json={'item_id':self.item,'date':'2030-01-07','start':'09:00','end':'10:00'},headers=headers).status_code
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(book,range(6)))
        self.assertEqual(results.count(201),1,results)
        self.assertEqual(results.count(409),5,results)
        self.assertEqual(self.count('booking_reservations'),1)

    def test_T06_cancel_frees_slot_and_keeps_record(self):
        b=self.reserve()
        r=self.s.post(f"/api/reservations/{b['id']}/cancel",json={},headers=self.sh)
        self.assertEqual(r.status_code,200,r.json())
        self.assertEqual(self.records()['records'][0]['status'],'cancelled')
        self.reserve()
        self.assertEqual(self.count('booking_reservations'),2)

    def test_T07_other_booking_cancel_denied(self):
        b=self.reserve()
        r=self.other.post(f"/api/reservations/{b['id']}/cancel",json={},headers=self.oh)
        self.assertEqual(r.status_code,403)
        self.assertEqual(self.records()['records'][0]['status'],'confirmed')

    def test_T08_start_within_reservation(self):
        b=self.reserve();self.now=self.now.replace(hour=9)
        u=self.start(b)
        self.assertEqual(u['status'],'in_use')
        self.assertEqual(self.s.get(f'/api/items/{self.item}').json()['item']['status'],'in_use')

    def test_T09_return_preserves_home_location(self):
        b=self.reserve();self.now=self.now.replace(hour=9);u=self.start(b)
        self.now=self.now.replace(minute=40);returned=self.returned(u)
        self.assertEqual(returned['status'],'returned')
        self.assertEqual(returned['returned_at'],app.packed(self.now))
        item=self.s.get(f'/api/items/{self.item}').json()['item']
        self.assertEqual(item['home_location'],'2층 공용실')
        self.assertEqual(item['status'],'available')

    def test_T10_public_return_status_without_user_identity(self):
        b=self.reserve();self.now=self.now.replace(hour=9);self.returned(self.start(b))
        item=self.other.get(f'/api/items/{self.item}').json()['item']
        self.assertEqual(item['status'],'available')
        self.assertTrue(item['last_returned_at'])
        self.assertNotIn('user_id',item)
        self.assertNotIn('username',item)
        history=self.other.get(f'/api/items/{self.item}/history?from=2030-01-01&to=2030-01-31').json()
        self.assertEqual(history['records'],[])

    def test_T11_other_return_denied(self):
        b=self.reserve();self.now=self.now.replace(hour=9);u=self.start(b)
        self.returned(u,self.other,self.oh,expected=403)
        self.assertEqual(self.s.get(f'/api/items/{self.item}').json()['item']['status'],'in_use')

    def test_T12_repeat_return_is_idempotent(self):
        b=self.reserve();self.now=self.now.replace(hour=9);u=self.start(b)
        first=self.returned(u);count=self.count('booking_events')
        self.now=self.now.replace(minute=30);second=self.returned(u)
        self.assertEqual(first,second)
        self.assertEqual(self.count('booking_events'),count)
        self.assertEqual(self.count('booking_usages'),1)

    def test_T13_previous_unreturned_blocks_next(self):
        first=self.reserve();second=self.reserve('10:00','11:00',self.other,self.oh)
        self.now=self.now.replace(hour=9);self.start(first)
        self.now=self.now.replace(hour=10,minute=1)
        self.start(second,self.other,self.oh,409)
        self.assertTrue(self.other.get(f'/api/items/{self.item}').json()['item']['is_overdue'])

    def test_T14_failure_rolls_back_return_and_audit(self):
        b=self.reserve();self.now=self.now.replace(hour=9);u=self.start(b)
        before=self.count('booking_events')
        with patch.object(app,'record_event',side_effect=app.psycopg.DatabaseError('forced failure')):
            self.returned(u,expected=503)
        self.assertEqual(self.count('booking_events'),before)
        row=self.records()['records'][0]
        self.assertIsNone(row['returned_at'])
        self.assertEqual(row['usage_status'],'in_use')
        self.assertEqual(self.s.get(f'/api/items/{self.item}').json()['item']['status'],'in_use')

    def test_T15_ai_failure_keeps_original_records(self):
        self.reserve()
        r=self.admin.post('/api/summary?from=2030-01-01&to=2030-01-31',json={},headers=self.ah)
        self.assertEqual(r.status_code,200,r.json())
        self.assertEqual(r.json()['status'],'unavailable')
        self.assertEqual(r.json()['items'][0]['reservation_count'],1)
        self.assertEqual(len(self.admin.get(r.json()['evidence_url']).json()['records']),1)
        self.assertEqual(self.s.get('/api/items').status_code,200)

    def test_T16_reopen_storage_preserves_all_data(self):
        b=self.reserve();self.now=self.now.replace(hour=9);u=self.start(b);self.returned(u)
        second=app.create_app(self.config)
        with app.app_context(second):
            app.initialize(seed=False)
            with app.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) AS n FROM booking_reservations').fetchone()['n'],1)
                self.assertEqual(db.execute('SELECT status FROM booking_usages').fetchone()['status'],'returned')
                self.assertEqual(db.execute('SELECT count(*) AS n FROM booking_items').fetchone()['n'],4)

    def test_invalid_reservation_inputs(self):
        for start,end,status in [('07:00','08:00',409),('06:00','07:00',409),('09:00','11:30',400),('09:15','10:00',400),('10:00','09:00',400)]:
            self.reserve(start,end,expected=status)
        self.reserve(date='not-date',expected=400)
        self.reserve(date='2029-01-01',expected=409)
        self.assertEqual(self.count('booking_reservations'),0)

    def test_start_replay_outside_time_and_cancellation_after_use(self):
        b=self.reserve();self.start(b,expected=409)
        self.now=self.now.replace(hour=9);u=self.start(b)
        repeated=self.start(b,expected=200)
        self.assertEqual(u,repeated)
        self.assertEqual(self.count('booking_usages'),1)
        self.assertEqual(self.s.post(f"/api/reservations/{b['id']}/cancel",json={},headers=self.sh).status_code,409)
        self.now=self.now.replace(hour=11);self.assertEqual(self.start(b,expected=200),u)

    def test_admin_exception_return_and_no_location_input(self):
        b=self.reserve();self.now=self.now.replace(hour=9);u=self.start(b)
        self.returned(u,data={'location':'임의 장소'},expected=400)
        self.returned(u,self.admin,self.ah,expected=400)
        self.returned(u,self.admin,self.ah,{'reason':'지정 보관 장소에서 반납 확인'})
        row=self.admin.get('/api/events?from=2030-01-01&to=2030-01-31').json()['records'][0]
        self.assertEqual(row['action'],'admin_returned')
        self.assertEqual(self.s.get(f'/api/items/{self.item}').json()['item']['home_location'],'2층 공용실')

    def test_admin_catalog_schedule_conflict_and_duplicate(self):
        self.reserve()
        item=self.s.get(f'/api/items/{self.item}').json()['item']
        data={k:item[k] for k in ('code','name','category','description','home_location','version')}
        data['hours']=[{'weekday':0,'open':'10:00','close':'23:00'}]
        self.assertEqual(self.admin.put(f'/api/items/{self.item}',json=data,headers=self.ah).status_code,409)
        data['hours']=[{'weekday':n,'open':'07:00','close':'23:00'} for n in range(7)]
        self.assertEqual(self.admin.put(f'/api/items/{self.item}',json=data,headers=self.ah).status_code,200)
        self.assertEqual(self.admin.put(f'/api/items/{self.item}',json=data,headers=self.ah).status_code,409)
        self.assertEqual(self.admin.post('/api/items',json=data,headers=self.ah).status_code,409)
        data['code']='NEW-001'
        self.assertEqual(self.admin.post('/api/items',json=data,headers=self.ah).status_code,201)
        self.assertEqual(self.s.post('/api/items',json=data,headers=self.sh).status_code,403)
        data['hours']=[{'weekday':0,'open':'23:00','close':'22:00'}]
        self.assertEqual(self.admin.post('/api/items',json=data,headers=self.ah).status_code,400)

    def test_availability_closed_booked_past_and_adjacent(self):
        self.reserve()
        r=self.s.get(f'/api/items/{self.item}/availability?date=2030-01-07')
        slots={s['start']:s for s in r.json()['slots']}
        self.assertEqual(slots['06:00']['reason'],'운영 시간 외')
        self.assertEqual(slots['07:00']['reason'],'지난 시간')
        self.assertEqual(slots['09:00']['reason'],'내 예약')
        self.assertTrue(slots['10:00']['available'])

    def test_ai_empty_privacy_and_late_statistics(self):
        path='/api/summary?from=2030-01-01&to=2030-01-31'
        with patch.object(app.ai_client,'generate_summary') as llm:
            self.assertEqual(self.admin.post(path,json={},headers=self.ah).json()['status'],'empty')
            llm.assert_not_called()
        b=self.reserve();self.now=self.now.replace(hour=9);u=self.start(b)
        self.now=self.now.replace(hour=10,minute=15);self.returned(u)
        with patch.object(app.ai_client,'generate_summary',return_value='요약') as llm:
            result=self.admin.post(path,json={},headers=self.ah).json()
            self.assertEqual(result['status'],'ok')
            self.assertEqual(result['items'][0]['late_return_count'],1)
            self.assertEqual(result['items'][0]['registered_use_minutes'],75)
            self.assertEqual(result['busy_hours_minutes']['09:00'],60)
            self.assertNotIn('username',str(llm.call_args))
            self.assertNotIn('2층 공용실',str(llm.call_args))
            self.assertNotIn('무선 청소기',str(llm.call_args))

    def test_local_reader_does_not_block_reservation_write(self):
        if not self.config['LOCAL_DB']:
            self.skipTest('SQLite WAL regression only')
        def write_booking():
            with TestClient(self.web, raise_server_exceptions=False) as client:
                login=client.post('/api/login',json={'username':'student','password':'student123!'})
                return client.post('/api/reservations',json={'item_id':self.item,'date':'2030-01-07','start':'09:00','end':'10:00'},
                    headers={'X-CSRF-Token':login.json()['csrf_token']}).status_code
        with app.app_context(self.web):
            with app.connect() as reader:
                reader.execute('SELECT * FROM booking_items').fetchall()
                # Keep a reader snapshot open while a different connection commits a booking.
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    result=pool.submit(write_booking).result(timeout=3)
                self.assertEqual(result,201)
        self.assertEqual(self.count('booking_reservations'),1)

    def test_catalog_defaults_and_matching_photos(self):
        records=self.s.get('/api/items').json()['items']
        by_name={r['name']:r for r in records}
        self.assertEqual(set(by_name),{'청소기','택배 카트','스팀 다리미','공용 우산'})
        self.assertEqual(by_name['스팀 다리미']['home_location'],'2층 공용실')
        for name in ['vacuum','steamer','umbrella','cart']:
            result = self.s.get(f'/static/images/{name}.jpg')
            self.assertEqual(result.status_code,200)
            self.assertTrue(result.headers['content-type'].startswith('image/'))

    def test_catalog_migration_preserves_existing_reservations(self):
        self.reserve()
        with app.app_context(self.web):
            with app.connect(write=True) as db:
                db.execute('DELETE FROM booking_catalog_updates')
                db.execute("UPDATE booking_items SET name='무선 청소기' WHERE code='CLEAN-001'")
                db.execute("UPDATE booking_items SET name='다리미',home_location='3층 세탁실' WHERE code='IRON-001'")
                db.execute("UPDATE booking_items SET code='SPORT-001',name='배드민턴 라켓',category='운동용품' WHERE code='UMBRELLA-001'")
                racket=db.execute("SELECT id FROM booking_items WHERE code='SPORT-001'").fetchone()['id']
        cleaner=self.item;self.item=racket;self.reserve();self.item=cleaner
        with app.app_context(self.web):
            app.initialize(seed=False)
            with app.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) AS n FROM booking_reservations').fetchone()['n'],2)
                self.assertEqual(app.get_item(db,racket)['name'],'배드민턴 라켓')
                self.assertTrue(app.is_retired(db,racket))
        names={i['name'] for i in self.s.get('/api/items').json()['items']}
        self.assertEqual(names,{'청소기','택배 카트','스팀 다리미','공용 우산'})
        self.item=racket;self.reserve('10:00','11:00',expected=409);self.item=cleaner
        event_count=self.count('booking_events')
        with app.app_context(self.web):
            app.initialize(seed=False)
        self.assertEqual(self.count('booking_events'),event_count)
        self.assertEqual(self.records()['records'][-1]['item_id'],cleaner)

    def set_quantity(self,quantity):
        with app.app_context(self.web):
            with app.connect(write=True) as db:
                db.execute('UPDATE booking_items SET quantity=%s WHERE id=%s',(quantity,self.item))

    def item_state(self):
        return self.s.get(f'/api/items/{self.item}').json()['item']

    def test_inventory_default_quantities_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            config={**self.config,'LOCAL_DB':str(Path(tmp)/'fresh.sqlite3'),'DATABASE_URL':''}
            web=app.create_app(config)
            with patch.dict(os.environ,{'DEMO_STUDENT_PASSWORD':'student123!','DEMO_ADMIN_PASSWORD':'admin123!'}):
                with app.app_context(web):
                    app.initialize(seed=True)
                    with app.connect() as db:
                        quantities={r['code']:r['quantity'] for r in db.execute('SELECT code,quantity FROM booking_items').fetchall()}
                    self.assertEqual(quantities,app.DEFAULT_QUANTITIES)
                    with app.connect(write=True) as db:
                        db.execute("UPDATE booking_items SET quantity=5 WHERE code='CLEAN-001'")
                    app.initialize(seed=True)
                    with app.connect() as db:
                        self.assertEqual(db.execute("SELECT quantity FROM booking_items WHERE code='CLEAN-001'").fetchone()['quantity'],5)

    def test_three_reservations_use_capacity_not_binary_status(self):
        self.set_quantity(3)
        b1=self.reserve()
        b2=self.reserve(client=self.other,headers=self.oh)
        b3=self.reserve(client=self.admin,headers=self.ah)
        self.now=self.now.replace(hour=9)
        state=self.item_state()
        self.assertEqual(state['status'],'in_use')
        self.assertEqual((state['quantity'],state['occupied_count'],state['available_count']),(3,3,0))
        self.other.post(f"/api/reservations/{b2['id']}/cancel",json={},headers=self.oh)
        state=self.item_state()
        self.assertEqual(state['status'],'available')
        self.assertEqual(state['available_count'],1)
        self.admin.post(f"/api/reservations/{b3['id']}/cancel",json={},headers=self.ah)
        self.start(b1)
        state=self.item_state()
        self.assertEqual((state['status'],state['active_count'],state['available_count']),('available',1,2))

    def test_concurrent_capacity_never_oversells(self):
        self.set_quantity(3)
        with app.app_context(self.web):
            with app.connect(write=True) as db:
                hashed=db.execute("SELECT password_hash FROM booking_users WHERE username='student'").fetchone()['password_hash']
                for n in range(6):
                    db.execute("INSERT INTO booking_users(username,password_hash,role) VALUES(%s,%s,'student')",(f'capacity{n}',hashed))
        def book(n):
            client,headers=self.login(f'capacity{n}')
            return client.post('/api/reservations',json={'item_id':self.item,'date':'2030-01-07','start':'09:00','end':'10:00'},headers=headers).status_code
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(book,range(6)))
        self.assertEqual(results.count(201),3,results)
        self.assertEqual(results.count(409),3,results)
        self.assertEqual(self.count('booking_reservations'),3)

    def test_availability_remaining_counts_and_own_booking(self):
        self.set_quantity(3)
        self.reserve()
        def slot(client):
            return next(s for s in client.get(f'/api/items/{self.item}/availability?date=2030-01-07').json()['slots'] if s['start']=='09:00')
        self.assertEqual(slot(self.other)['remaining'],2)
        self.assertTrue(slot(self.other)['available'])
        self.assertEqual(slot(self.s)['reason'],'내 예약')
        self.reserve(client=self.other,headers=self.oh)
        self.assertEqual(slot(self.admin)['remaining'],1)
        self.reserve(client=self.admin,headers=self.ah)
        client,_=self.login('student')
        self.assertEqual(slot(client)['remaining'],0)
        self.assertFalse(slot(client)['available'])

    def test_disjoint_selection_saves_only_selected_times_atomically(self):
        payload={'item_id':self.item,'date':'2030-01-07','intervals':[{'start':'09:00','end':'09:30'},{'start':'10:00','end':'10:30'}]}
        r=self.s.post('/api/reservations',json=payload,headers=self.sh)
        self.assertEqual(r.status_code,201,r.json())
        self.assertEqual(len(r.json()['reservations']),2)
        self.reserve('09:30','10:00',client=self.other,headers=self.oh)
        self.assertEqual(self.count('booking_reservations'),3)
        # First segment is free, second is occupied. Neither segment may be persisted on failure.
        payload['intervals']=[{'start':'11:00','end':'11:30'},{'start':'09:30','end':'10:00'}]
        before=self.count('booking_reservations')
        self.assertEqual(self.admin.post('/api/reservations',json=payload,headers=self.ah).status_code,409)
        self.assertEqual(self.count('booking_reservations'),before)

    def test_partial_overlap_capacity_checks_peak_not_total_overlap(self):
        self.set_quantity(2)
        self.reserve('09:00','09:30')
        self.reserve('09:30','10:00',client=self.other,headers=self.oh)
        self.reserve('09:00','10:00',client=self.admin,headers=self.ah)
        self.assertEqual(self.count('booking_reservations'),3)

    def test_multiple_active_units_and_late_return_release_one_unit(self):
        self.set_quantity(3)
        bookings=[self.reserve(),self.reserve(client=self.other,headers=self.oh),self.reserve(client=self.admin,headers=self.ah)]
        self.now=self.now.replace(hour=9)
        usages=[self.start(bookings[0]),self.start(bookings[1],self.other,self.oh),self.start(bookings[2],self.admin,self.ah)]
        self.assertEqual(self.item_state()['active_count'],3)
        self.now=self.now.replace(hour=10,minute=1)
        self.assertEqual(self.item_state()['status'],'in_use')
        self.returned(usages[0])
        self.assertEqual(self.item_state()['status'],'available')
        self.assertEqual(self.item_state()['available_count'],1)
        self.assertTrue(self.item_state()['is_overdue'])

    def test_quantity_reduction_cannot_break_existing_capacity(self):
        self.set_quantity(3)
        self.reserve();self.reserve(client=self.other,headers=self.oh)
        item=self.item_state()
        data={k:item[k] for k in ('code','name','category','description','home_location','version')}
        data.update(quantity=1,hours=[{'weekday':n,'open':'07:00','close':'23:00'} for n in range(7)])
        self.assertEqual(self.admin.put(f'/api/items/{self.item}',json=data,headers=self.ah).status_code,409)
        self.assertEqual(self.item_state()['quantity'],3)
        data['quantity']=2
        self.assertEqual(self.admin.put(f'/api/items/{self.item}',json=data,headers=self.ah).status_code,200)

    def test_overdue_notification_deadline_and_owner(self):
        booking=self.reserve()
        self.now=self.now.replace(hour=9)
        usage=self.start(booking)
        before=self.s.get('/api/notifications').json()
        self.assertEqual(before['count'],0)
        self.assertEqual(before['next_due_at'],booking['end_at'])
        self.now=self.now.replace(hour=10)
        self.assertEqual(self.s.get('/api/notifications').json()['count'],0)
        self.now=self.now.replace(second=1)
        result=self.s.get('/api/notifications').json()
        self.assertEqual(result['count'],1)
        alert=result['notifications'][0]
        self.assertEqual(alert['usage_id'],usage['id'])
        self.assertEqual(alert['item_name'],'청소기')
        self.assertEqual(alert['overdue_seconds'],1)
        self.assertNotIn('user_id',alert)
        self.assertNotIn('username',alert)
        self.assertEqual(self.other.get('/api/notifications').json()['count'],0)
        self.assertEqual(TestClient(self.web, raise_server_exceptions=False).get('/api/notifications').status_code,401)

    def test_return_clears_overdue_notification_without_changing_time(self):
        booking=self.reserve();self.now=self.now.replace(hour=9);usage=self.start(booking)
        self.now=self.now.replace(hour=10,minute=15)
        first=self.s.get('/api/notifications').json()
        repeated=self.s.get('/api/notifications').json()
        self.assertEqual(first['notifications'][0]['id'],repeated['notifications'][0]['id'])
        self.assertEqual(first['notifications'][0]['overdue_seconds'],900)
        self.returned(usage)
        result=self.s.get('/api/notifications').json()
        self.assertEqual(result['count'],0)
        self.assertIsNone(result['next_due_at'])
        self.assertEqual(result['notifications'],[])

    def test_unused_or_cancelled_bookings_do_not_prompt_return(self):
        unused=self.reserve()
        cancelled=self.reserve('10:00','11:00')
        self.s.post(f"/api/reservations/{cancelled['id']}/cancel",json={},headers=self.sh)
        self.now=self.now.replace(hour=12)
        result=self.s.get('/api/notifications').json()
        self.assertEqual(result['count'],0)
        self.assertIsNone(result['next_due_at'])
        self.assertEqual(self.count('booking_usages'),0)

    def test_fastapi_schema_and_request_validation(self):
        schema = self.s.get('/openapi.json').json()
        operation = schema['paths']['/api/reservations']['post']
        model = operation['requestBody']['content']['application/json']['schema']
        self.assertTrue(model['$ref'].endswith('/ReservationRequest'))
        self.assertTrue(any(p['name'] == 'X-CSRF-Token' for p in operation['parameters']))
        self.assertEqual(self.s.get('/docs').status_code, 200)
        invalid = self.s.post('/api/reservations', json={'item_id': str(self.item), 'date': '2030-01-07'}, headers=self.sh)
        self.assertEqual(invalid.status_code, 400)
        self.assertIn('item_id', invalid.json()['error'])
        self.assertEqual(self.s.post('/api/login', json={'username': 'x' * 25000}).status_code, 413)
        self.assertEqual(self.s.post('/api/login', json={}, headers={'Origin': 'https://example.invalid'}).status_code, 403)

    def test_login_csrf_private_records_and_assets(self):
        self.assertEqual(TestClient(self.web, raise_server_exceptions=False).get('/api/items').status_code,401)
        self.assertEqual(self.s.post('/api/reservations',json={}).status_code,403)
        self.assertEqual(self.s.get('/api/reservations?all=1').status_code,403)
        self.assertEqual(self.s.get('/api/events').status_code,403)
        self.assertEqual(self.s.post('/api/summary',json={},headers=self.sh).status_code,403)
        for path in ['/', '/static/app.js', '/static/style.css']:
            self.assertEqual(self.s.get(path).status_code,200)
        self.assertEqual(self.s.get('/api/reservations?from=bad').status_code,400)
        self.reserve()
        self.assertEqual(self.records(self.other)['records'],[])
        self.assertNotIn('username',self.records()['records'][0])


if __name__=='__main__':unittest.main()
