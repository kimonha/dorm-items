"""기숙사 공용 물품 예약·사용·반납 서비스.

로컬 실행: ./start.command 또는 python -m uvicorn main:app
PostgreSQL: 환경 변수 설정 후 python app.py init-db / python app.py run
Docker: docker compose up --build -d
"""
import argparse
from contextlib import contextmanager
import datetime as dt
from contextvars import ContextVar
import logging
from typing import Annotated
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
import psycopg
from psycopg.rows import dict_row
from werkzeug.security import generate_password_hash, check_password_hash

import ai_client

ROOT=Path(__file__).resolve().parent
KST=ZoneInfo('Asia/Seoul')
UTC=dt.timezone.utc

SCHEMA='''
CREATE TABLE IF NOT EXISTS booking_users (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 username VARCHAR(80) UNIQUE NOT NULL,
 password_hash TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('student','admin'))
);
CREATE TABLE IF NOT EXISTS booking_sessions (
 token_hash TEXT PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES booking_users(id),
 csrf_token TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS booking_items (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 code VARCHAR(40) UNIQUE NOT NULL, name VARCHAR(100) NOT NULL,
 category VARCHAR(60) NOT NULL, description VARCHAR(2000) NOT NULL,
 home_location VARCHAR(150) NOT NULL, quantity INTEGER NOT NULL DEFAULT 1 CHECK(quantity BETWEEN 1 AND 1000), version INTEGER NOT NULL DEFAULT 1,
 updated_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS booking_hours (
 item_id BIGINT NOT NULL REFERENCES booking_items(id),
 weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
 open_min INTEGER NOT NULL CHECK(open_min>=0 AND open_min<1440 AND open_min%30=0),
 close_min INTEGER NOT NULL CHECK(close_min>0 AND close_min<=1440 AND close_min%30=0),
 PRIMARY KEY(item_id,weekday), CHECK(open_min<close_min)
);
CREATE TABLE IF NOT EXISTS booking_reservations (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 item_id BIGINT NOT NULL REFERENCES booking_items(id),
 user_id BIGINT NOT NULL REFERENCES booking_users(id),
 start_at TIMESTAMPTZ NOT NULL, end_at TIMESTAMPTZ NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('confirmed','cancelled')),
 created_at TIMESTAMPTZ NOT NULL, cancelled_at TIMESTAMPTZ,
 CHECK(start_at<end_at),
 CHECK((status='confirmed' AND cancelled_at IS NULL) OR (status='cancelled' AND cancelled_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS booking_overlap ON booking_reservations(item_id,status,start_at,end_at);
CREATE TABLE IF NOT EXISTS booking_usages (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 reservation_id BIGINT UNIQUE NOT NULL REFERENCES booking_reservations(id),
 item_id BIGINT NOT NULL REFERENCES booking_items(id),
 status TEXT NOT NULL CHECK(status IN ('in_use','returned')),
 started_at TIMESTAMPTZ NOT NULL, returned_at TIMESTAMPTZ,
 CHECK((status='in_use' AND returned_at IS NULL) OR (status='returned' AND returned_at>=started_at))
);
CREATE INDEX IF NOT EXISTS booking_active_items ON booking_usages(item_id) WHERE returned_at IS NULL;
CREATE TABLE IF NOT EXISTS booking_events (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 item_id BIGINT NOT NULL REFERENCES booking_items(id),
 reservation_id BIGINT REFERENCES booking_reservations(id),
 usage_id BIGINT REFERENCES booking_usages(id),
 actor_id BIGINT NOT NULL REFERENCES booking_users(id),
 action TEXT NOT NULL CHECK(action IN ('registered','edited','reserved','cancelled','started','returned','admin_returned')),
 old_status TEXT, new_status TEXT, note VARCHAR(1000) NOT NULL DEFAULT '',
 detail TEXT NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS booking_event_time ON booking_events(item_id,created_at);
CREATE TABLE IF NOT EXISTS booking_catalog_updates (
 version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS booking_retired_items (
 item_id BIGINT PRIMARY KEY REFERENCES booking_items(id), retired_at TIMESTAMPTZ NOT NULL
);

'''


class APIError(Exception):
    def __init__(self,message,status=400):
        self.message,self.status=message,status


settings = ContextVar('dorm_settings')
logger = logging.getLogger(__name__)


@contextmanager
def app_context(web):
    token = settings.set(web.state.config)
    try:
        yield
    finally:
        settings.reset(token)


def clock():
    provider=settings.get().get('CLOCK')
    return (provider() if provider else dt.datetime.now(UTC)).astimezone(UTC)


def timestamp(value):
    if isinstance(value,dt.datetime):
        return value.astimezone(UTC)
    parsed=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def packed(value):
    if isinstance(value,dt.datetime):
        return value.astimezone(UTC).isoformat(timespec='microseconds').replace('+00:00','Z')
    if isinstance(value,(list,tuple)):
        return [packed(x) for x in value]
    if isinstance(value,dict):
        return {k:packed(v) for k,v in value.items()}
    return value


class LocalDB:
    """SQLite runner: serialize writes before validation; readers use WAL snapshots."""
    def __init__(self,path,write=False):
        self.db=sqlite3.connect(path,timeout=15)
        self.db.row_factory=self.row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA busy_timeout=15000')
        self.db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        self.db.create_function('strpos',2,lambda value,term:value.find(term)+1)

    @staticmethod
    def row(cursor,values):
        row={d[0]:value for d,value in zip(cursor.description,values)}
        for key,value in row.items():
            if value is not None and key.endswith('_at'):
                row[key]=timestamp(value)
        return row

    def execute(self,sql,params=()):
        if sql==SCHEMA:
            sql=sql.replace('BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY','INTEGER PRIMARY KEY AUTOINCREMENT')
            for statement in sql.split(';'):
                if statement.strip():
                    self.db.execute(statement)
            return self.db.execute('SELECT 1')
        if sql.startswith('SELECT pg_advisory'):
            return self.db.execute('SELECT 1')
        sql=sql.replace('%s','?').replace(' FOR UPDATE','')
        try:
            return self.db.execute(sql,[packed(x) for x in params])
        except sqlite3.IntegrityError as exc:
            if 'UNIQUE' in str(exc):
                raise psycopg.errors.UniqueViolation('conflicting record') from exc
            raise psycopg.DatabaseError('local integrity error') from exc
        except sqlite3.Error as exc:
            raise psycopg.DatabaseError('local query error') from exc


@contextmanager
def connect(write=False):
    path=settings.get().get('LOCAL_DB')
    if path:
        connection=LocalDB(path,write=write)
        try:
            yield connection
            connection.db.commit()
        except BaseException:
            connection.db.rollback()
            raise
        finally:
            connection.db.close()
    else:
        with psycopg.connect(settings.get()['DATABASE_URL'] or '',row_factory=dict_row,
                             connect_timeout=5,options='-c timezone=UTC -c statement_timeout=10000 -c lock_timeout=5000') as connection:
            yield connection


def record_event(db,item_id,action,old=None,new=None,reservation_id=None,usage_id=None,note='',detail=None,*,actor_id):
    db.execute('''INSERT INTO booking_events(item_id,reservation_id,usage_id,actor_id,action,old_status,new_status,note,detail,created_at)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
               (item_id,reservation_id,usage_id,actor_id,action,old,new,note,json.dumps(packed(detail or {}),ensure_ascii=False),clock()))


def update_catalog(db):
    """One-time defaults update; preserve reservations and historical equipment identity."""
    version='catalog-photos-20261109'
    if db.execute('SELECT version FROM booking_catalog_updates WHERE version=%s',(version,)).fetchone():
        return
    admin=db.execute("SELECT id FROM booking_users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
    updates=[('CLEAN-001','무선 청소기',{'name':'청소기','description':'먼지통을 비우고 전원 코드를 정리해 지정 보관 장소에 반납해 주세요.'}),
             ('IRON-001','다리미',{'name':'스팀 다리미','home_location':'2층 공용실','description':'사용 후 물통을 비우고 전원을 끈 뒤 충분히 식혀서 반납해 주세요.'})]
    for code,old_name,changes in updates:
        item=db.execute('SELECT * FROM booking_items WHERE code=%s AND name=%s',(code,old_name)).fetchone()
        if not item:
            continue
        fields=','.join(k+'=%s' for k in changes)
        db.execute('UPDATE booking_items SET '+fields+',version=version+1,updated_at=%s WHERE id=%s',(*changes.values(),clock(),item['id']))
        if admin:
            record_event(db,item['id'],'edited',actor_id=admin['id'],detail={'before':{k:item[k] for k in changes},'after':changes})
    old=db.execute("SELECT * FROM booking_items WHERE code='SPORT-001' AND name='배드민턴 라켓'").fetchone()
    if old:
        # This is different equipment: do not relabel any previous racket reservations as umbrella usage.
        db.execute('INSERT INTO booking_retired_items VALUES(%s,%s) ON CONFLICT(item_id) DO NOTHING',(old['id'],clock()))
        umbrella=db.execute("SELECT id FROM booking_items WHERE code='UMBRELLA-001'").fetchone()
        if not umbrella:
            umbrella=db.execute("""INSERT INTO booking_items(code,name,category,description,home_location,updated_at)
                VALUES('UMBRELLA-001','공용 우산','생활용품','비 오는 날 함께 사용하는 우산입니다. 사용 후 물기를 털고 우산꽂이에 반납해 주세요.','1층 로비 우산꽂이',%s) RETURNING id""",(clock(),)).fetchone()
            for h in hours_for(db,old['id']):
                db.execute('INSERT INTO booking_hours VALUES(%s,%s,%s,%s)',(umbrella['id'],h['weekday'],h['open_min'],h['close_min']))
            if admin:
                record_event(db,umbrella['id'],'registered',new='available',actor_id=admin['id'])
    db.execute('INSERT INTO booking_catalog_updates VALUES(%s,%s)',(version,clock()))


def is_retired(db,item_id):
    return bool(db.execute('SELECT item_id FROM booking_retired_items WHERE item_id=%s',(item_id,)).fetchone())


DEFAULT_QUANTITIES={'CLEAN-001':3,'CART-001':8,'IRON-001':3,'UMBRELLA-001':8}


def migrate_quantities(db):
    if settings.get().get('LOCAL_DB'):
        exists=any(row['name']=='quantity' for row in db.execute('PRAGMA table_info(booking_items)').fetchall())
    else:
        exists=bool(db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='booking_items' AND column_name='quantity'").fetchone())
    if not exists:
        db.execute('ALTER TABLE booking_items ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1 CHECK(quantity BETWEEN 1 AND 1000)')
    # A reservation still has one usage record, but different units may be used concurrently.
    db.execute('DROP INDEX IF EXISTS booking_one_active')
    version='inventory-quantities-20261109'
    if not db.execute('SELECT version FROM booking_catalog_updates WHERE version=%s',(version,)).fetchone():
        for code,quantity in DEFAULT_QUANTITIES.items():
            db.execute('UPDATE booking_items SET quantity=%s,version=version+1,updated_at=%s WHERE code=%s',(quantity,clock(),code))
        db.execute('INSERT INTO booking_catalog_updates VALUES(%s,%s)',(version,clock()))


    correction='inventory-steamer-three-20261109'
    if not db.execute('SELECT version FROM booking_catalog_updates WHERE version=%s',(correction,)).fetchone():
        item=db.execute("SELECT * FROM booking_items WHERE code='IRON-001'").fetchone()
        if item and item['quantity']!=3:
            db.execute('UPDATE booking_items SET quantity=3,version=version+1,updated_at=%s WHERE id=%s',(clock(),item['id']))
            admin=db.execute("SELECT id FROM booking_users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
            if admin:
                record_event(db,item['id'],'edited',actor_id=admin['id'],note='보유 수량을 3개로 정정',detail={'before':{'quantity':item['quantity']},'after':{'quantity':3}})
        db.execute('INSERT INTO booking_catalog_updates VALUES(%s,%s)',(correction,clock()))


def capacity_bookings(db,item_id,start,end):
    return db.execute('''SELECT r.id,r.user_id,r.start_at,r.end_at FROM booking_reservations r
        LEFT JOIN booking_usages us ON us.reservation_id=r.id
        WHERE r.item_id=%s AND r.status='confirmed' AND r.start_at<%s AND r.end_at>%s
        AND us.returned_at IS NULL''',(item_id,end,start)).fetchall()


def peak_occupancy(bookings,start,end):
    events=[]
    for r in bookings:
        a,b=max(start,r['start_at']),min(end,r['end_at'])
        if a<b:
            events.extend([(a,1),(b,-1)])
    current=peak=0
    # At a shared boundary, release ending reservations before counting new ones.
    for _,change in sorted(events):
        current+=change
        peak=max(peak,current)
    return peak


def timetable(db,item,day,user):
    hours=hours_for(db,item['id']);retired=is_retired(db,item['id']);now=clock()
    bookings=capacity_bookings(db,item['id'],moment(day,0),moment(day,1440))
    result=[]
    for n in range(0,1440,30):
        start,end=moment(day,n),moment(day,n+30)
        count=peak_occupancy(bookings,start,end)
        remaining=max(0,item['quantity']-count)
        mine=any(r['user_id']==user['id'] and start<r['end_at'] and end>r['start_at'] for r in bookings)
        reason=''
        if retired:reason='제공 종료'
        elif not within_hours(start,end,hours):reason='운영 시간 외'
        elif start<now:reason='지난 시간'
        elif mine:reason='내 예약'
        elif remaining==0:reason='예약 마감'
        result.append({'start':time_label(n),'end':time_label(n+30),'available':not reason,'reason':reason,
                       'reserved':count,'remaining':remaining,'quantity':item['quantity'],'is_mine':mine})
    return result


def initialize(seed=False):
    path=settings.get().get('LOCAL_DB')
    if path:
        connection=sqlite3.connect(path,timeout=15)
        try:
            connection.execute('PRAGMA journal_mode=WAL')
        finally:
            connection.close()
    with connect(write=True) as db:
        db.execute('SELECT pg_advisory_xact_lock(91826732)')
        db.execute(SCHEMA)
        update_catalog(db)
        migrate_quantities(db)
        if not seed:
            return
        student=os.environ.get('DEMO_STUDENT_PASSWORD')
        admin=os.environ.get('DEMO_ADMIN_PASSWORD')
        if not student or not admin:
            raise RuntimeError('DEMO_STUDENT_PASSWORD와 DEMO_ADMIN_PASSWORD를 설정하세요.')
        for username,password,role in [('student',student,'student'),('student2',student,'student'),('admin',admin,'admin')]:
            db.execute('''INSERT INTO booking_users(username,password_hash,role) VALUES(%s,%s,%s)
                          ON CONFLICT(username) DO NOTHING''',(username,generate_password_hash(password),role))
        admin_id=db.execute("SELECT id FROM booking_users WHERE username='admin'").fetchone()['id']
        samples=[('CLEAN-001','청소기','청소용품','먼지통을 비우고 전원 코드를 정리해 지정 보관 장소에 반납해 주세요.','2층 공용실'),
                 ('CART-001','택배 카트','운반용품','무거운 택배를 옮길 때 사용하세요.','1층 로비'),
                 ('IRON-001','스팀 다리미','생활용품','사용 후 물통을 비우고 전원을 끈 뒤 충분히 식혀서 반납해 주세요.','2층 공용실'),
                 ('UMBRELLA-001','공용 우산','생활용품','비 오는 날 함께 사용하는 우산입니다. 사용 후 물기를 털고 우산꽂이에 반납해 주세요.','1층 로비 우산꽂이')]
        for code,name,category,description,home in samples:
            item=db.execute('''INSERT INTO booking_items(code,name,category,description,home_location,quantity,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(code) DO NOTHING RETURNING id''',
                (code,name,category,description,home,DEFAULT_QUANTITIES.get(code,1),clock())).fetchone()
            if item:
                for day in range(7):
                    db.execute('INSERT INTO booking_hours VALUES(%s,%s,420,1380)',(item['id'],day))
                record_event(db,item['id'],'registered',new='available',actor_id=admin_id)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def authenticated_user(
    request: Request,
    csrf_token: Annotated[str | None, Header(alias='X-CSRF-Token')] = None,
):
    with connect() as db:
        user = db.execute(
            """SELECT u.id,u.username,u.role,s.csrf_token FROM booking_sessions s
               JOIN booking_users u ON u.id=s.user_id
               WHERE s.token_hash=%s AND s.expires_at>%s""",
            (token_hash(request.cookies.get('dorm_session','')), clock()),
        ).fetchone()
    if not user:
        raise APIError('로그인이 필요합니다.', 401)
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        if not hmac.compare_digest(user['csrf_token'], csrf_token or ''):
            raise APIError('요청 인증에 실패했습니다. 다시 로그인해 주세요.', 403)
    return user


User = Annotated[dict, Depends(authenticated_user)]


def administrator(user: User):
    if user['role'] != 'admin':
        raise APIError('관리자만 사용할 수 있습니다.', 403)
    return user


Admin = Annotated[dict, Depends(administrator)]


class LoginRequest(BaseModel):
    model_config = ConfigDict(strict=True)
    username: str
    password: str


class OperatingHours(BaseModel):
    model_config = ConfigDict(strict=True)
    weekday: int
    open: str
    close: str


class ItemRequest(BaseModel):
    model_config = ConfigDict(strict=True)
    code: str
    name: str
    category: str
    description: str
    home_location: str
    quantity: int = Field(default=1, ge=1, le=1000)
    hours: list[OperatingHours] = Field(min_length=1, max_length=7)


class ItemUpdate(ItemRequest):
    version: int = Field(gt=0)


class ReservationInterval(BaseModel):
    model_config = ConfigDict(strict=True)
    start: str
    end: str


class ReservationRequest(BaseModel):
    model_config = ConfigDict(strict=True)
    item_id: int = Field(gt=0)
    date: str
    start: str | None = None
    end: str | None = None
    intervals: list[ReservationInterval] | None = None


class ReturnRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra='allow')
    reason: str | None = None


def reply(data, status=200):
    return JSONResponse(packed(data), status_code=status)


class RequestPolicy:
    """Set per-request configuration, enforce write limits and attach response headers."""

    def __init__(self, app, config):
        self.app = app
        self.config = config

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        token = settings.set(self.config)

        async def send_response(message):
            if message['type'] == 'http.response.start':
                headers = MutableHeaders(scope=message)
                headers['X-Content-Type-Options'] = 'nosniff'
                headers['X-Frame-Options'] = 'DENY'
                headers['Referrer-Policy'] = 'same-origin'
                if request.url.path == '/docs':
                    headers['Content-Security-Policy'] = (
                        "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                        "img-src 'self' data: https://fastapi.tiangolo.com; frame-ancestors 'none'"
                    )
                else:
                    headers['Content-Security-Policy'] = (
                        "default-src 'self'; script-src 'self'; style-src 'self'; "
                        "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
                        "base-uri 'none'; form-action 'self'"
                    )
                if request.url.path.startswith('/api/'):
                    headers['Cache-Control'] = 'no-store'
            await send(message)

        try:
            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                origin = request.headers.get('Origin')
                expected = str(request.base_url).rstrip('/')
                if origin and origin.rstrip('/') != expected:
                    raise APIError('허용되지 않은 요청 출처입니다.', 403)
                media_type = request.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
                if media_type != 'application/json' and not (
                    media_type.startswith('application/') and media_type.endswith('+json')
                ):
                    raise APIError('JSON 형식이 필요합니다.', 415)
                raw = bytearray()
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > self.config['MAX_CONTENT_LENGTH']:
                        raise APIError('입력이 너무 큽니다.', 413)
                    raw.extend(chunk)
                original_receive = receive
                pending = True

                async def replay():
                    nonlocal pending
                    if pending:
                        pending = False
                        return {'type': 'http.request', 'body': bytes(raw), 'more_body': False}
                    return await original_receive()

                receive = replay
            await self.app(scope, receive, send_response)
        except APIError as error:
            await reply({'error': error.message}, error.status)(scope, receive, send_response)
        finally:
            settings.reset(token)


def text(data,key,limit):
    value=data.get(key)
    if not isinstance(value,str) or not value.strip() or len(value.strip())>limit:
        raise APIError(f'{key}: 1~{limit}자의 값을 입력해 주세요.')
    return value.strip()


def integer(data,key):
    value=data.get(key)
    if type(value) is not int or value<=0:
        raise APIError(f'{key}: 올바른 번호가 필요합니다.')
    return value


def date_value(value):
    try:
        parsed=dt.date.fromisoformat(value)
        if parsed.isoformat()!=value:
            raise ValueError()
        return parsed
    except (ValueError,TypeError):
        raise APIError('날짜는 YYYY-MM-DD 형식으로 입력해 주세요.')


def minute(value,allow_midnight=False):
    if not isinstance(value,str) or not re.fullmatch(r'\d{2}:\d{2}',value):
        raise APIError('시간은 HH:MM 형식으로 입력해 주세요.')
    hour,minutes=map(int,value.split(':'))
    if allow_midnight and hour==24 and minutes==0:
        return 1440
    if hour not in range(24) or minutes not in (0,30):
        raise APIError('시간은 30분 단위로 입력해 주세요.')
    return hour*60+minutes


def time_label(value):
    return f'{value//60:02d}:{value%60:02d}'


def moment(day,minutes):
    return (dt.datetime.combine(day,dt.time.min,KST)+dt.timedelta(minutes=minutes)).astimezone(UTC)


def period(request: Request):
    today=clock().astimezone(KST).date()
    start=date_value(request.query_params.get('from',(today-dt.timedelta(days=29)).isoformat()))
    end=date_value(request.query_params.get('to',today.isoformat()))
    if end<start or (end-start).days>365:
        raise APIError('조회 기간은 시작일 이후 최대 366일까지입니다.')
    return start.isoformat(),end.isoformat(),moment(start,0),moment(end+dt.timedelta(days=1),0)


def pagination(request: Request):
    try:
        page=int(request.query_params.get('page','1'))
    except ValueError:
        raise APIError('페이지 번호를 확인해 주세요.')
    if not 1<=page<=100000:
        raise APIError('페이지 번호를 확인해 주세요.')
    return page,50


def get_item(db,item_id,lock=False):
    item=db.execute('SELECT * FROM booking_items WHERE id=%s'+(' FOR UPDATE' if lock else ''),(item_id,)).fetchone()
    if not item:
        raise APIError('물품을 찾을 수 없습니다.',404)
    return item


def hours_for(db,item_id):
    return db.execute('SELECT weekday,open_min,close_min FROM booking_hours WHERE item_id=%s ORDER BY weekday',(item_id,)).fetchall()


def public_item(db,item):
    now=clock()
    active=db.execute("""SELECT us.id,us.reservation_id,us.started_at,r.end_at FROM booking_usages us
        JOIN booking_reservations r ON r.id=us.reservation_id WHERE us.item_id=%s AND us.returned_at IS NULL""",(item['id'],)).fetchall()
    current=capacity_bookings(db,item['id'],now,now+dt.timedelta(microseconds=1))
    occupied={r['id'] for r in current}|{r['reservation_id'] for r in active}
    remaining=max(0,item['quantity']-len(occupied))
    last=db.execute('SELECT returned_at FROM booking_usages WHERE item_id=%s AND returned_at IS NOT NULL ORDER BY returned_at DESC LIMIT 1',(item['id'],)).fetchone()
    return {**item,'status':'available' if remaining>0 else 'in_use','available_count':remaining,
            'occupied_count':len(occupied),'active_count':len(active),'reserved_count':len(current),
            'last_returned_at':last['returned_at'] if last else None,
            'is_overdue':any(r['end_at']<now for r in active),'hours':hours_for(db,item['id'])}


def catalog(data):
    result={k:text(data,k,n) for k,n in [('code',40),('name',100),('category',60),('description',2000),('home_location',150)]}
    quantity=data.get('quantity',1)
    if type(quantity) is not int or not 1<=quantity<=1000:
        raise APIError('수량은 1~1,000 사이의 정수로 입력해 주세요.')
    result['quantity']=quantity
    result['code']=result['code'].upper()
    if not re.fullmatch(r'[A-Z0-9][A-Z0-9_-]{0,39}',result['code']):
        raise APIError('물품 코드는 영문·숫자·하이픈·밑줄만 사용할 수 있습니다.')
    raw=data.get('hours')
    if not isinstance(raw,list) or not raw or len(raw)>7:
        raise APIError('운영 요일·시간을 하나 이상 입력해 주세요.')
    hours=[]
    seen=set()
    for entry in raw:
        if not isinstance(entry,dict) or type(entry.get('weekday')) is not int or entry['weekday'] not in range(7) or entry['weekday'] in seen:
            raise APIError('운영 요일은 월요일 0부터 일요일 6까지 중복 없이 입력해 주세요.')
        opening=minute(entry.get('open'))
        closing=minute(entry.get('close'),True)
        if opening>=closing:
            raise APIError('운영 종료는 시작보다 늦어야 합니다. 자정을 넘는 운영은 지원하지 않습니다.')
        hours.append({'weekday':entry['weekday'],'open_min':opening,'close_min':closing})
        seen.add(entry['weekday'])
    return result,hours


def within_hours(start,end,hours):
    local=start.astimezone(KST)
    for h in hours:
        if h['weekday']==local.weekday() and moment(local.date(),h['open_min'])<=start and end<=moment(local.date(),h['close_min']):
            return True
    return False


def booking_record(row,user,include_owner=False):
    row=dict(row)
    row['is_mine']=row.pop('user_id')==user['id']
    if not include_owner:
        row.pop('username',None)
    active=row.get('usage_id') and row.get('returned_at') is None
    row['is_overdue']=bool(active and clock()>row['end_at'])
    row['was_late']=bool(row.get('returned_at') and row['returned_at']>row['end_at'])
    row['can_start']=bool(row['is_mine'] and row['status']=='confirmed' and not row.get('usage_id') and row['start_at']<=clock()<row['end_at'])
    row['can_cancel']=bool(row['is_mine'] and row['status']=='confirmed' and not row.get('usage_id'))
    row['can_return']=bool(active and (row['is_mine'] or user['role']=='admin'))
    return row


BOOKING_SELECT='''SELECT r.*,i.name AS item_name,i.code,i.home_location,u.username,
    us.id AS usage_id,us.status AS usage_status,us.started_at,us.returned_at
    FROM booking_reservations r JOIN booking_items i ON i.id=r.item_id
    JOIN booking_users u ON u.id=r.user_id LEFT JOIN booking_usages us ON us.reservation_id=r.id'''


def booking_rows(db,begin,end,user,all_users=False,item_id=None):
    conditions=['r.start_at<%s','r.end_at>%s']
    params=[end,begin]
    if not all_users:
        conditions.append('r.user_id=%s');params.append(user['id'])
    if item_id is not None:
        conditions.append('r.item_id=%s');params.append(item_id)
    return BOOKING_SELECT+' WHERE '+' AND '.join(conditions),params


def create_app(config=None, *, lifespan=None):
    web = FastAPI(
        title='기숙사 공용 물품 관리 API',
        description='POST /api/login에서 로그인한 뒤, 변경 요청의 X-CSRF-Token에 로그인 응답의 csrf_token을 입력하세요.',
        redoc_url=None,
        lifespan=lifespan,
    )
    web.state.config = dict(
        DATABASE_URL=os.environ.get('DATABASE_URL', ''),
        LOCAL_DB=os.environ.get('DORM_LOCAL_DB', ''),
        COOKIE_SECURE=os.environ.get('COOKIE_SECURE', 'false').lower() == 'true',
        GEMINI_API_KEY=os.environ.get('GEMINI_API_KEY', ''),
        GEMINI_MODEL=os.environ.get('GEMINI_MODEL', 'gemini-3.5-flash-lite'),
        AI_TIMEOUT_SECONDS=float(os.environ.get('AI_TIMEOUT_SECONDS', '15')),
        MAX_CONTENT_LENGTH=24000,
    )
    if config:
        web.state.config.update(config)
    web.add_middleware(RequestPolicy, config=web.state.config)
    web.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
    dummy_hash = generate_password_hash('invalid-login-password')

    @web.exception_handler(APIError)
    async def known(request: Request, error: APIError):
        return reply({'error': error.message}, error.status)

    @web.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        fields = ['.'.join(map(str, e['loc'][1:])) for e in error.errors() if len(e['loc']) > 1]
        message = '요청 형식이 올바르지 않습니다.'
        if fields:
            message += ' 입력 항목: ' + ', '.join(dict.fromkeys(fields))
        return reply({'error': message}, 400)

    @web.exception_handler(psycopg.errors.UniqueViolation)
    async def conflict(request: Request, error):
        return reply({'error': '물품 코드가 중복되거나 이미 처리된 요청입니다. 최신 정보를 확인해 주세요.'}, 409)

    @web.exception_handler(psycopg.Error)
    async def db_error(request: Request, error):
        logger.error('Database failure: %s', type(error).__name__)
        return reply({'error': '데이터 저장 또는 조회에 실패했습니다. 진행 중 변경은 취소되었습니다. 다시 시도해 주세요.'}, 503)

    @web.exception_handler(HTTPException)
    async def http_error(request: Request, error):
        message = {400:'잘못된 요청입니다.',404:'요청한 페이지를 찾을 수 없습니다.',405:'지원하지 않는 요청 방식입니다.',413:'입력이 너무 큽니다.',415:'JSON 형식이 필요합니다.'}.get(error.status_code,'요청을 처리할 수 없습니다.')
        response = reply({'error': message}, error.status_code)
        if error.headers:
            response.headers.update(error.headers)
        return response

    @web.exception_handler(Exception)
    async def unexpected(request: Request, error):
        logger.error('Unhandled failure', exc_info=error)
        return reply({'error': '서버 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.'}, 500)

    @web.get('/', include_in_schema=False)
    def index():
        return FileResponse(ROOT / 'static' / 'index.html')

    @web.get('/health')
    def health():
        with connect() as db:
            db.execute('SELECT id FROM booking_items LIMIT 1')
        return reply({'status':'ok','version':'reservation-v2','database':'sqlite' if web.state.config['LOCAL_DB'] else 'postgresql'})

    @web.post('/api/login')
    def login(request: Request, payload: LoginRequest):
        data=payload.model_dump(exclude_unset=True);username=text(data,'username',80);password=text(data,'password',200)
        with connect(write=True) as db:
            user=db.execute('SELECT * FROM booking_users WHERE username=%s',(username,)).fetchone()
            valid=check_password_hash(user['password_hash'] if user else dummy_hash,password)
            if not user or not valid:
                raise APIError('아이디 또는 비밀번호를 확인해 주세요.',401)
            db.execute('DELETE FROM booking_sessions WHERE expires_at<=%s',(clock(),))
            db.execute('DELETE FROM booking_sessions WHERE token_hash=%s',(token_hash(request.cookies.get('dorm_session','')),))
            token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
            db.execute('INSERT INTO booking_sessions VALUES(%s,%s,%s,%s)',(token_hash(token),user['id'],csrf,clock()+dt.timedelta(hours=12)))
        res=reply({'user':{k:user[k] for k in ('id','username','role')},'csrf_token':csrf})
        res.set_cookie('dorm_session',token,httponly=True,samesite='Strict',secure=web.state.config['COOKIE_SECURE'],max_age=43200)
        return res

    @web.get('/api/me')
    def me(user: User):
        return reply({'user':{k:user[k] for k in ('id','username','role')},'csrf_token':user['csrf_token'],'server_time':clock()})

    @web.post('/api/logout')
    def logout(request: Request, user: User, payload: dict):
        with connect(write=True) as db:
            db.execute('DELETE FROM booking_sessions WHERE token_hash=%s',(token_hash(request.cookies.get('dorm_session','')),))
        res=reply({'message':'로그아웃했습니다.'});res.delete_cookie('dorm_session',httponly=True,samesite='Strict',secure=web.state.config['COOKIE_SECURE'])
        return res

    @web.get('/api/items')
    def list_items(user: User, q: str = '', category: str = ''):
        q=q.strip()[:100];category=category.strip()[:60]
        with connect() as db:
            items=db.execute('''SELECT * FROM booking_items WHERE (strpos(lower(name),lower(%s))>0 OR strpos(lower(category),lower(%s))>0)
                AND (%s='' OR category=%s) AND NOT EXISTS (SELECT 1 FROM booking_retired_items retired WHERE retired.item_id=booking_items.id) ORDER BY id''',(q,q,category,category)).fetchall()
            items=[public_item(db,i) for i in items]
            categories=[r['category'] for r in db.execute('SELECT DISTINCT category FROM booking_items WHERE NOT EXISTS (SELECT 1 FROM booking_retired_items retired WHERE retired.item_id=booking_items.id) ORDER BY category').fetchall()]
        return reply({'items':items,'categories':categories,'server_time':clock()})

    @web.get('/api/items/{item_id}')
    def item_detail(item_id: int, user: User):
        with connect() as db:
            item=public_item(db,get_item(db,item_id))
        return reply({'item':item,'server_time':clock()})

    @web.post('/api/items', status_code=201)
    def add_item(payload: ItemRequest, user: Admin):
        values,hours=catalog(payload.model_dump())
        with connect(write=True) as db:
            item=db.execute('''INSERT INTO booking_items(code,name,category,description,home_location,quantity,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *''',(*values.values(),clock())).fetchone()
            for h in hours:
                db.execute('INSERT INTO booking_hours VALUES(%s,%s,%s,%s)',(item['id'],h['weekday'],h['open_min'],h['close_min']))
            record_event(db,item['id'],'registered',new='available',detail={'fields':values,'hours':hours},actor_id=user['id'])
        return reply({'item':item},201)

    @web.put('/api/items/{item_id}')
    def edit_item(item_id: int, payload: ItemUpdate, user: Admin):
        data=payload.model_dump(exclude_unset=True);values,hours=catalog(data);version=integer(data,'version')
        with connect(write=True) as db:
            item=get_item(db,item_id,True)
            if item['version']!=version:
                raise APIError('다른 사용자가 수정했습니다. 최신 정보를 다시 조회해 주세요.',409)
            existing=db.execute("SELECT * FROM booking_reservations WHERE item_id=%s AND status='confirmed' AND end_at>%s",(item_id,clock())).fetchall()
            if any(not within_hours(r['start_at'],r['end_at'],hours) for r in existing):
                raise APIError('기존 유효 예약을 운영 시간 밖으로 만드는 변경은 저장할 수 없습니다.',409)
            ongoing=db.execute('SELECT count(*) AS n FROM booking_usages WHERE item_id=%s AND returned_at IS NULL',(item_id,)).fetchone()['n']
            future=capacity_bookings(db,item_id,clock(),dt.datetime.max.replace(tzinfo=UTC))
            peak=peak_occupancy(future,clock(),max([r['end_at'] for r in future],default=clock()))
            if values['quantity']<max(ongoing,peak):
                raise APIError('기존 예약 또는 실제 사용 수량보다 적게 줄일 수 없습니다.',409)
            db.execute('''UPDATE booking_items SET code=%s,name=%s,category=%s,description=%s,home_location=%s,quantity=%s,
                version=version+1,updated_at=%s WHERE id=%s''',(*values.values(),clock(),item_id))
            old_hours=hours_for(db,item_id)
            db.execute('DELETE FROM booking_hours WHERE item_id=%s',(item_id,))
            for h in hours:
                db.execute('INSERT INTO booking_hours VALUES(%s,%s,%s,%s)',(item_id,h['weekday'],h['open_min'],h['close_min']))
            record_event(db,item_id,'edited',detail={'before':{k:item[k] for k in values},'after':values,'old_hours':old_hours,'hours':hours},actor_id=user['id'])
        return reply({'message':'물품 정보와 운영 시간을 저장했습니다.'})

    @web.get('/api/items/{item_id}/availability')
    def availability(item_id: int, user: User, date: str = ''):
        day=date_value(date)
        with connect() as db:
            item=get_item(db,item_id)
            slots=timetable(db,item,day,user)
        return reply({'item_id':item_id,'date':day.isoformat(),'slots':slots,'quantity':item['quantity'],'server_time':clock()})

    @web.post('/api/reservations', status_code=201)
    def reserve(payload: ReservationRequest, user: User):
        data=payload.model_dump(exclude_unset=True);item_id=integer(data,'item_id');day=date_value(data.get('date'))
        raw=data.get('intervals',[{'start':data.get('start'),'end':data.get('end')}])
        if not isinstance(raw,list) or not 1<=len(raw)<=4:
            raise APIError('예약 구간을 선택해 주세요. 한 번에 최대 2시간까지 가능합니다.')
        intervals=[]
        for entry in raw:
            if not isinstance(entry,dict):raise APIError('예약 구간을 확인해 주세요.')
            a,b=minute(entry.get('start')),minute(entry.get('end'),True)
            if b<=a:raise APIError('종료 시간은 시작보다 늦어야 합니다.')
            intervals.append((a,b))
        intervals.sort()
        if sum(b-a for a,b in intervals)>120 or any(intervals[i][0]<intervals[i-1][1] for i in range(1,len(intervals))):
            raise APIError('예약 구간은 중복 없이 합계 최대 2시간까지 가능합니다.')
        with connect(write=True) as db:
            item=get_item(db,item_id,True)
            if is_retired(db,item_id):raise APIError('더 이상 제공하지 않는 물품입니다.',409)
            hours=hours_for(db,item_id)
            # Validate every segment under the same lock; never save a partial selection.
            for a,b in intervals:
                start,end=moment(day,a),moment(day,b)
                if start<clock():raise APIError('과거 시간은 예약할 수 없습니다.',409)
                if not within_hours(start,end,hours):raise APIError('운영 요일과 시간 안에서 예약해 주세요.',409)
                existing=capacity_bookings(db,item_id,start,end)
                if any(r['user_id']==user['id'] for r in existing):
                    raise APIError('같은 물품을 해당 시간에 이미 예약했습니다.',409)
                if peak_occupancy(existing,start,end)>=item['quantity']:
                    raise APIError('해당 시간의 물품 수량이 모두 예약되었습니다. 다른 시간을 선택해 주세요.',409)
            saved=[]
            for a,b in intervals:
                r=db.execute("INSERT INTO booking_reservations(item_id,user_id,start_at,end_at,status,created_at) VALUES(%s,%s,%s,%s,'confirmed',%s) RETURNING *",(item_id,user['id'],moment(day,a),moment(day,b),clock())).fetchone()
                record_event(db,item_id,'reserved',new='confirmed',reservation_id=r['id'],actor_id=user['id'])
                saved.append(r)
            result={'reservation':saved[0],'reservations':saved,'slots':timetable(db,item,day,user),
                    'item':public_item(db,item),'server_time':clock(),'message':'선택한 예약을 저장했습니다.'}
        return reply(result,201)

    @web.get('/api/reservations')
    def reservations(request: Request, user: User):
        start,end,begin,until=period(request);page,size=pagination(request)
        all_users=request.query_params.get('all')=='1'
        if all_users and user['role']!='admin':
            raise APIError('전체 기록은 관리자만 조회할 수 있습니다.',403)
        raw_item_id=request.query_params.get('item_id','')
        item_id=int(raw_item_id) if raw_item_id.isascii() and raw_item_id.isdecimal() and len(raw_item_id)<=18 else None
        with connect() as db:
            sql,params=booking_rows(db,begin,until,user,all_users,item_id)
            total=db.execute('SELECT count(*) AS n FROM ('+sql+') AS result',params).fetchone()['n']
            rows=db.execute(sql+' ORDER BY r.start_at DESC,r.id DESC LIMIT %s OFFSET %s',(*params,size,(page-1)*size)).fetchall()
            rows=[booking_record(r,user,all_users) for r in rows]
        return reply({'records':rows,'total':total,'page':page,'page_size':size,'from':start,'to':end,'server_time':clock()})

    @web.get('/api/my-active')
    def active_records(user: User):
        with connect() as db:
            rows=db.execute(BOOKING_SELECT+' WHERE r.user_id=%s AND us.returned_at IS NULL AND us.id IS NOT NULL ORDER BY r.start_at DESC',(user['id'],)).fetchall()
        return reply({'records':[booking_record(r,user) for r in rows]})

    @web.get('/api/notifications')
    def notifications(user: User):
        now=clock()
        with connect() as db:
            rows=db.execute(BOOKING_SELECT+                ' WHERE r.user_id=%s AND us.id IS NOT NULL AND us.returned_at IS NULL ORDER BY r.end_at,r.id',
                (user['id'],)).fetchall()
            pending=db.execute(BOOKING_SELECT+
                " WHERE r.user_id=%s AND r.status='confirmed' AND us.id IS NULL"
                " AND r.end_at>%s ORDER BY r.start_at,r.id",
                (user['id'],now)).fetchall()
        reminders=[]
        next_reminders=[]
        for row in pending:
            due=row['start_at']-dt.timedelta(minutes=5)
            if due>now:
                next_reminders.append(due)
                continue
            ready=row['start_at']<=now
            if not ready:
                next_reminders.append(row['start_at'])
            reminders.append({
                'id':f"reservation-{row['id']}-{'start' if ready else 'soon'}",
                'type':'reservation','reservation_id':row['id'],'ready':ready,
                'item_name':row['item_name'],'start_at':row['start_at'],
                'message':row['item_name']+(' 예약 시간이 시작됐어요. 지금 사용하시겠어요?' if ready else ' 예약 시작까지 5분 이내예요. 사용을 준비해 주세요.'),
            })
        alerts=[]
        for row in rows:
            if row['end_at']>=now:
                continue
            alerts.append({'id':f"overdue-{row['usage_id']}",'type':'overdue',
                'usage_id':row['usage_id'],'reservation_id':row['id'],'item_id':row['item_id'],
                'item_name':row['item_name'],'home_location':row['home_location'],
                'end_at':row['end_at'],'started_at':row['started_at'],
                'overdue_seconds':int((now-row['end_at']).total_seconds()),
                'message':row['item_name']+'의 예약 시간이 지났습니다. 지정 장소에 반납한 뒤 반납 완료를 눌러 주세요.'})
        upcoming=[row['end_at'] for row in rows if row['end_at']>=now]
        return reply({'notifications':alerts,'count':len(alerts),'server_time':now,
                      'next_due_at':min(upcoming+next_reminders) if upcoming or next_reminders else None,
                      'reservation_reminders':reminders})

    @web.post('/api/reservations/{reservation_id}/cancel')
    def cancel(reservation_id: int, user: User, payload: dict):
        with connect(write=True) as db:
            r=db.execute('SELECT * FROM booking_reservations WHERE id=%s',(reservation_id,)).fetchone()
            if not r:raise APIError('예약을 찾을 수 없습니다.',404)
            get_item(db,r['item_id'],True)
            r=db.execute('SELECT * FROM booking_reservations WHERE id=%s',(reservation_id,)).fetchone()
            if r['user_id']!=user['id']:raise APIError('본인의 예약만 취소할 수 있습니다.',403)
            if db.execute('SELECT id FROM booking_usages WHERE reservation_id=%s',(reservation_id,)).fetchone():
                raise APIError('이미 사용을 시작한 예약은 취소할 수 없습니다.',409)
            if r['status']=='cancelled':return reply({'message':'이미 취소된 예약입니다.','reservation':r})
            db.execute("UPDATE booking_reservations SET status='cancelled',cancelled_at=%s WHERE id=%s",(clock(),reservation_id))
            record_event(db,r['item_id'],'cancelled','confirmed','cancelled',reservation_id,actor_id=user['id'])
        return reply({'message':'예약을 취소했습니다.'})

    @web.post('/api/reservations/{reservation_id}/start')
    def start_usage(reservation_id: int, user: User, payload: dict):
        with connect(write=True) as db:
            r=db.execute('SELECT * FROM booking_reservations WHERE id=%s',(reservation_id,)).fetchone()
            if not r:raise APIError('예약을 찾을 수 없습니다.',404)
            item=get_item(db,r['item_id'],True)
            r=db.execute('SELECT * FROM booking_reservations WHERE id=%s',(reservation_id,)).fetchone()
            if r['user_id']!=user['id']:raise APIError('본인의 예약만 사용을 시작할 수 있습니다.',403)
            previous=db.execute('SELECT * FROM booking_usages WHERE reservation_id=%s',(reservation_id,)).fetchone()
            if previous:return reply({'usage':previous,'message':'이미 처리한 사용 시작 요청입니다.'})
            if r['status']!='confirmed':raise APIError('취소된 예약은 사용할 수 없습니다.',409)
            if not r['start_at']<=clock()<r['end_at']:raise APIError('본인의 예약 시간에만 사용을 시작할 수 있습니다.',409)
            active=db.execute('SELECT count(*) AS n FROM booking_usages WHERE item_id=%s AND returned_at IS NULL',(r['item_id'],)).fetchone()['n']
            if active>=item['quantity']:
                raise APIError('모든 물품이 사용 중입니다. 이전 이용자의 반납 완료를 기다려 주세요.',409)
            usage=db.execute("INSERT INTO booking_usages(reservation_id,item_id,status,started_at) VALUES(%s,%s,'in_use',%s) RETURNING *",(reservation_id,r['item_id'],clock())).fetchone()
            db.execute('UPDATE booking_items SET updated_at=%s WHERE id=%s',(clock(),r['item_id']))
            record_event(db,r['item_id'],'started','confirmed','in_use',reservation_id,usage['id'],actor_id=user['id'])
        return reply({'usage':usage,'message':'사용을 시작했습니다.'},201)

    @web.post('/api/usages/{usage_id}/return')
    def return_usage(usage_id: int, payload: ReturnRequest, user: User):
        data=payload.model_dump(exclude_unset=True)
        if any(k in data for k in ('location','home_location','return_location')):
            raise APIError('반납 위치는 입력하지 않습니다. 지정 보관 장소에 반납해 주세요.')
        with connect(write=True) as db:
            usage=db.execute('''SELECT us.*,r.user_id FROM booking_usages us JOIN booking_reservations r ON r.id=us.reservation_id WHERE us.id=%s''',(usage_id,)).fetchone()
            if not usage:raise APIError('이용 기록을 찾을 수 없습니다.',404)
            get_item(db,usage['item_id'],True)
            usage=db.execute('''SELECT us.*,r.user_id FROM booking_usages us JOIN booking_reservations r ON r.id=us.reservation_id WHERE us.id=%s''',(usage_id,)).fetchone()
            other=usage['user_id']!=user['id']
            if other and user['role']!='admin':raise APIError('본인의 이용 건만 반납 완료를 등록할 수 있습니다.',403)
            if usage['returned_at'] is not None:
                usage.pop('user_id')
                return reply({'usage':usage,'message':'이미 반납 완료된 이용 건입니다.'})
            note=text(data,'reason',1000) if other else ''
            returned=clock()
            db.execute("UPDATE booking_usages SET status='returned',returned_at=%s WHERE id=%s",(returned,usage_id))
            db.execute('UPDATE booking_items SET updated_at=%s WHERE id=%s',(returned,usage['item_id']))
            record_event(db,usage['item_id'],'admin_returned' if other else 'returned','in_use','returned',usage['reservation_id'],usage_id,note,actor_id=user['id'])
            usage=db.execute('SELECT * FROM booking_usages WHERE id=%s',(usage_id,)).fetchone()
        return reply({'usage':usage,'message':'지정 장소에 반납했다고 등록했습니다.'})

    @web.get('/api/items/{item_id}/history')
    def item_history(item_id: int, request: Request, user: User):
        start,end,begin,until=period(request);page,size=pagination(request)
        with connect() as db:
            get_item(db,item_id)
            sql,params=booking_rows(db,begin,until,user,user['role']=='admin',item_id)
            total=db.execute('SELECT count(*) AS n FROM ('+sql+') AS result',params).fetchone()['n']
            rows=db.execute(sql+' ORDER BY r.start_at DESC,r.id DESC LIMIT %s OFFSET %s',(*params,size,(page-1)*size)).fetchall()
            rows=[booking_record(r,user,user['role']=='admin') for r in rows]
        return reply({'records':rows,'total':total,'page':page,'page_size':size,'from':start,'to':end})

    @web.get('/api/events')
    def events(request: Request, user: Admin):
        start,end,begin,until=period(request);page,size=pagination(request)
        with connect() as db:
            total=db.execute('SELECT count(*) AS n FROM booking_events WHERE created_at>=%s AND created_at<%s',(begin,until)).fetchone()['n']
            rows=db.execute('''SELECT e.*,i.name,u.username FROM booking_events e JOIN booking_items i ON i.id=e.item_id
                JOIN booking_users u ON u.id=e.actor_id WHERE e.created_at>=%s AND e.created_at<%s
                ORDER BY e.created_at DESC,e.id DESC LIMIT %s OFFSET %s''',(begin,until,size,(page-1)*size)).fetchall()
        return reply({'records':rows,'total':total,'page':page,'page_size':size})

    @web.post('/api/summary')
    def summary(request: Request, user: Admin, payload: dict):
        start,end,begin,until=period(request)
        with connect() as db:
            sql,params=booking_rows(db,begin,until,user,True)
            rows=db.execute(sql+' ORDER BY r.id',params).fetchall()
        # All facts and evidence come from one query/snapshot, not a truncated page.
        now=clock();aggregates={};hours={};evidence=[]
        for r in rows:
            item=aggregates.setdefault(r['item_id'],{'item_id':r['item_id'],'name':r['item_name'],'reservation_count':0,'cancelled_count':0,'use_count':0,'returned_count':0,'overdue_count':0,'late_return_count':0,'registered_use_minutes':0})
            if r['status']=='cancelled':item['cancelled_count']+=1
            else:
                item['reservation_count']+=1
                cursor=max(r['start_at'],begin)
                stop=min(r['end_at'],until)
                while cursor<stop:
                    label=cursor.astimezone(KST).strftime('%H:00')
                    boundary=cursor.replace(minute=0,second=0,microsecond=0)+dt.timedelta(hours=1)
                    chunk=min(stop,boundary)
                    hours[label]=hours.get(label,0)+(chunk-cursor).total_seconds()/60
                    cursor=chunk
            if r['usage_id']:
                item['use_count']+=1
                if r['returned_at']:
                    item['returned_count']+=1
                    item['registered_use_minutes']+=(r['returned_at']-r['started_at']).total_seconds()/60
                    if r['returned_at']>r['end_at']:item['late_return_count']+=1
                elif now>r['end_at']:item['overdue_count']+=1
            evidence.append({k:r[k] for k in ('id','item_id','item_name','status','start_at','end_at','usage_id','started_at','returned_at')})
        items=list(aggregates.values())
        for item in items:item['registered_use_minutes']=round(item['registered_use_minutes'],1)
        evidence_url=f'/api/reservations?all=1&from={start}&to={end}'
        result={'from':start,'to':end,'as_of':now,'items':items,'busy_hours_minutes':hours,'record_count':len(rows),'evidence_url':evidence_url,
                'basis':'조회 기간과 겹치는 예약 기준.'}
        result['basis']='조회 기간과 겹치는 예약 기준. 예약 건수·시간대 통계는 취소 제외. 이용 시간은 사용 시작부터 반납 완료 등록까지의 시간이며, 실제 물리적 반납 시각은 아닙니다.'
        if not rows:return reply({**result,'status':'empty','summary':None,'message':'조회 기간에 예약·이용 기록이 없어 AI를 호출하지 않았습니다.'})
        if len(items)>500:return reply({**result,'status':'unavailable','summary':None,'message':'분석 대상이 너무 많습니다. 조회 기간을 줄여 주세요. 집계와 원본 기록은 계속 확인할 수 있습니다.'})
        # Only numeric IDs and aggregate numbers leave the server. Evidence is local.
        facts={'from':start,'to':end,'as_of':packed(now),'items':[{k:v for k,v in item.items() if k!='name'} for item in items],
               'busy_hours_reserved_minutes':hours,'time_basis':'reservation overlap with selected KST dates; cancelled excluded; use duration is registration-based'}
        try:
            answer = ai_client.generate_summary(
                facts,
                api_key=web.state.config['GEMINI_API_KEY'],
                model=web.state.config['GEMINI_MODEL'],
                timeout=web.state.config['AI_TIMEOUT_SECONDS'],
            )
            return reply({**result,'status':'ok','summary':answer})
        except ai_client.AIUnavailable as exc:
            return reply({**result,'status':'unavailable','summary':None,'message':str(exc)})

    return web


def initialize_local_demo(web):
    os.environ.setdefault('DEMO_STUDENT_PASSWORD', 'student123!')
    os.environ.setdefault('DEMO_ADMIN_PASSWORD', 'admin123!')
    with app_context(web):
        initialize(seed=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command')
    commands.add_parser('init-db')
    create=commands.add_parser('create-user');create.add_argument('username');create.add_argument('--role',choices=['student','admin'],default='student')
    run=commands.add_parser('run');run.add_argument('--port',type=int,default=8000)
    args=parser.parse_args()
    local=not os.environ.get('DATABASE_URL') and not os.environ.get('PGHOST')
    if local:
        os.environ.setdefault('DORM_LOCAL_DB',str(ROOT/'dorm-reservations.sqlite3'))
    web=create_app()
    with app_context(web):
        if args.command=='init-db':
            initialize(seed=os.environ.get('SEED_DEMO','false').lower()=='true')
            print('초기화 완료. 기존 데이터는 유지됩니다.')
        elif args.command=='create-user':
            if not args.username.strip() or len(args.username)>80:parser.error('계정은 1~80자입니다.')
            password=getpass.getpass('비밀번호 (12자 이상): ')
            if len(password)<12 or len(password)>200 or password!=getpass.getpass('비밀번호 확인: '):parser.error('비밀번호 길이 또는 확인 값이 올바르지 않습니다.')
            with connect() as db:
                db.execute('INSERT INTO booking_users(username,password_hash,role) VALUES(%s,%s,%s)',(args.username.strip(),generate_password_hash(password),args.role))
        else:
            if local:
                initialize_local_demo(web)
                print('학생: student / student123! · 관리자: admin / admin123!',flush=True)
            import uvicorn
            uvicorn.run(web,host='127.0.0.1',port=getattr(args,'port',8000))


if __name__=='__main__':main()
