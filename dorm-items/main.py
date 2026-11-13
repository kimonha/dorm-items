"""수업 실습과 같은 Uvicorn 실행 진입점. python -m uvicorn main:app"""

from contextlib import asynccontextmanager
import os

from app import ROOT, create_app, initialize_local_demo


@asynccontextmanager
async def lifespan(web):
    if web.state.config['LOCAL_DB']:
        initialize_local_demo(web)
    yield


local_config = {}
if not os.environ.get('DATABASE_URL') and not os.environ.get('PGHOST'):
    local_config['LOCAL_DB'] = os.environ.get(
        'DORM_LOCAL_DB', str(ROOT / 'dorm-reservations.sqlite3')
    )

app = create_app(local_config, lifespan=lifespan)
