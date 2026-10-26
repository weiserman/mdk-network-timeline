"""MDK network timeline - upload a Mobile Services HAR trace (+ optional MDK client log) and get the
timeline view. Behind a simple login; traces can be saved under a title in data/traces.db."""
import gzip
import hmac
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fasthtml.common import *
from starlette.responses import HTMLResponse, RedirectResponse

import timeline

HERE = Path(__file__).parent
MAX_BYTES = 25 * 1024 * 1024
APP_NAME = 'MDK network timeline'


def _load_env(path):
    """plash sources plash.env in production; mirror that for local runs."""
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            m = re.match(r'\s*export\s+(\w+)=(.*)', line)
            if m:
                os.environ.setdefault(m[1], m[2].strip().strip('"\''))


_load_env(HERE / 'plash.env')
APP_USER, APP_PWD = os.environ.get('APP_USER', ''), os.environ.get('APP_PWD', '')
PRODUCTION = bool(os.environ.get('PLASH_PRODUCTION'))
# No credentials: open on a local run, closed to everyone when deployed.
LOGIN = bool(APP_USER and APP_PWD) or PRODUCTION
DISPLAY_TZ = os.environ.get('DISPLAY_TZ', 'UTC')

# ---- Storage: data/ survives plash deploys ---------------------------------------------------------
DATA = HERE / 'data'
DATA.mkdir(exist_ok=True)
DB = DATA / 'traces.db'


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute('''CREATE TABLE IF NOT EXISTS traces (
        id INTEGER PRIMARY KEY, title TEXT NOT NULL UNIQUE COLLATE NOCASE, created TEXT NOT NULL,
        har_name TEXT, log_name TEXT, har BLOB NOT NULL, log BLOB, summary TEXT, action TEXT, download TEXT)''')
    return con


# ---- App + login -----------------------------------------------------------------------------------
def require_login(req, sess):
    if LOGIN and not sess.get('auth'):
        return RedirectResponse('/login', status_code=303)


app, rt = fast_app(
    pico=True, static_path=str(HERE / 'static'),   # no static files: keeps data/ and the template unserved
    secret_key=os.environ.get('SESSION_SECRET'), sess_https_only=PRODUCTION,
    before=Beforeware(require_login, skip=[r'/login']),
    hdrs=(Style('''
      main.container{max-width:900px}
      .hint{color:var(--pico-muted-color);font-size:.9rem}
      .err{color:#d03b3b;font-weight:600}.ok{color:#0ca30c;font-weight:600}
      .top{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
      .two{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}
      @media (max-width:640px){.two{grid-template-columns:1fr}}
      table.saved td,table.saved th{font-size:.85rem;white-space:nowrap}
      table.saved td.t{white-space:normal}
      table.saved form{display:inline;margin:0}
      table.saved button{padding:2px 10px;font-size:.8rem;margin:0}
    '''),))


@rt('/login')
def get():
    return login_page()


def login_page(error=None):
    return Title('Sign in - ' + APP_NAME), Main(
        H1(APP_NAME),
        P(error, cls='err') if error else None,
        Form(Label('Username', Input(name='username', autocomplete='username', required=True, autofocus=True)),
             Label('Password', Input(name='password', type='password', autocomplete='current-password', required=True)),
             Button('Sign in', type='submit'), method='post', action='/login'),
        cls='container', style='max-width:420px')


@rt('/login')
def post(username: str, password: str, sess):
    ok = bool(APP_USER and APP_PWD) and hmac.compare_digest(username.strip(), APP_USER) \
        and hmac.compare_digest(password, APP_PWD)
    if not ok:
        return login_page('Wrong username or password.')
    sess['auth'] = APP_USER
    return RedirectResponse('/', status_code=303)


@rt('/logout')
def get(sess):
    sess.pop('auth', None)
    return RedirectResponse('/login', status_code=303)


# ---- Home: upload form + saved traces --------------------------------------------------------------
def _when(iso):
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromisoformat(iso).astimezone(ZoneInfo(DISPLAY_TZ)).strftime('%d %b %Y %H:%M ') + DISPLAY_TZ
    except Exception:
        return iso[:16].replace('T', ' ') + ' UTC'


def _f(ms):
    return '-' if ms is None else (f'{ms / 1000:.1f} s' if ms >= 1000 else f'{ms} ms')


def saved_table():
    with db() as con:
        rows = con.execute('SELECT id, title, created, har_name, log_name, summary FROM traces ORDER BY created DESC').fetchall()
    if not rows:
        return P('No saved traces yet.', cls='hint')
    body = []
    for r in rows:
        s = json.loads(r['summary'] or '{}')
        body.append(Tr(
            Td(A(r['title'], href=f"/trace/{r['id']}"), cls='t'),
            Td(_when(r['created'])),
            Td(_f(s.get('action'))), Td(_f(s.get('sap'))), Td(_f(s.get('mobsvc'))),
            Td('HAR + log' if r['log_name'] else 'HAR'),
            Td(A('Download', href=f"/trace/{r['id']}?download=1"), ' ',
               Form(Button('Delete', cls='secondary outline', type='submit'), method='post',
                    action=f"/trace/{r['id']}/delete",
                    onsubmit=f"return confirm({json.dumps('Delete ' + r['title'] + '?')})"))))
    return Div(Table(Thead(Tr(Th('Title'), Th('Saved'), Th('Action to downloaded'), Th('SAP'), Th('Mobile Svc'),
                              Th('Files'), Th(''))), Tbody(*body), cls='saved'), style='overflow-x:auto')


def home(error=None, note=None):
    return Title(APP_NAME), Main(
        Div(H1(APP_NAME), A('Sign out', href='/logout') if LOGIN else None, cls='top'),
        P('Upload a Mobile Services network trace (HAR) and, optionally, the MDK client log of the same run. '
          'You get one timeline: every call split into SAP backend, Cloud Connector + network and Mobile Services, '
          'the offline-sync windows with their idle time, and the device log lines on the same clock.',
          cls='hint'),
        P(error, cls='err') if error else None,
        P(note, cls='ok') if note else None,
        Form(
            Label('Network trace (.har) - Mobile Services cockpit > Network Trace > Download',
                  Input(type='file', name='har', accept='.har,application/json', required=True)),
            Label('Client log (.json, optional) - Mobile Services cockpit > Client Logs > Download',
                  Input(type='file', name='clientlog', accept='.json,application/json')),
            Label('Title', Input(name='title', placeholder='e.g. QA - create order, Android')),
            Div(Label('Action call (optional URL pattern)',
                      Input(name='action', placeholder='auto: first POST/PATCH followed by a sync')),
                Label('Offline read to follow (optional URL pattern)',
                      Input(name='download', placeholder='auto: slowest entity read per sync')), cls='two'),
            Label(Input(type='checkbox', name='save', value='1'), 'Save this trace under its title'),
            Label(Input(type='checkbox', name='download_html', value='1'), 'Download the timeline as an HTML file'),
            Button('Build timeline', type='submit'),
            method='post', action='/timeline', enctype='multipart/form-data'),
        H2('Saved traces'),
        saved_table(),
        P('Files are processed in memory; only traces you choose to save are stored.', cls='hint'),
        cls='container')


@rt('/')
def get():
    return home()


# ---- Build / view timelines ------------------------------------------------------------------------
def _parse(raw, label):
    if len(raw) > MAX_BYTES:
        raise ValueError(f'{label} is larger than {MAX_BYTES // (1024 * 1024)} MB.')
    try:
        return json.loads(raw.decode('utf-8-sig'))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f'{label} is not valid JSON ({e}).')


def respond(html, title, download, banner=''):
    if download:
        name = re.sub(r'[^\w.-]+', '_', title)[:80] or 'timeline'
        return HTMLResponse(html, headers={'Content-Disposition': f'attachment; filename="{name}.html"'})
    back = ('<p style="margin:0 0 12px;display:flex;gap:16px"><a href="/" style="color:var(--ink2)">&larr; Back</a>'
            + banner + '</p>')
    return HTMLResponse(html.replace('<body><main>', '<body><main>' + back, 1))


@rt('/timeline')
async def post(req):
    form = await req.form()
    har_file, log_file = form.get('har'), form.get('clientlog')
    title = (form.get('title') or '').strip()
    action, download = (form.get('action') or '').strip() or None, (form.get('download') or '').strip() or None
    try:
        if not getattr(har_file, 'filename', None):
            raise ValueError('Choose a HAR file.')
        har_raw = await har_file.read()
        log_raw = await log_file.read() if getattr(log_file, 'filename', None) else None
        har = _parse(har_raw, 'The HAR file')
        log = _parse(log_raw, 'The client log') if log_raw else None
        data = timeline.build(har, log, title=title or 'MDK app - network timeline', har_name=har_file.filename,
                              log_name=log_file.filename if log else '', action=action, download=download)
        html = timeline.render(data)
    except (ValueError, KeyError, TypeError) as e:
        return home(f'Could not build the timeline: {e}')

    banner = ''
    if form.get('save'):
        if not title:
            return home('Enter a title to save the trace.')
        summary = dict(data['stats'])
        if data['milestones']:
            summary['action'] = data['milestones'][0]['to'] - data['milestones'][0]['from']
        try:
            with db() as con:
                tid = con.execute(
                    'INSERT INTO traces (title, created, har_name, log_name, har, log, summary, action, download) '
                    'VALUES (?,?,?,?,?,?,?,?,?)',
                    (title, datetime.now(timezone.utc).isoformat(), har_file.filename,
                     log_file.filename if log else None, gzip.compress(har_raw),
                     gzip.compress(log_raw) if log else None, json.dumps(summary), action, download)).lastrowid
        except sqlite3.IntegrityError:
            return home(f'A trace called "{title}" is already saved - choose another title or delete the old one.')
        banner = f'<span style="color:var(--good)">Saved as "{escape(title)}"</span> <a href="/trace/{tid}">Permalink</a>'
    return respond(html, title or 'timeline', form.get('download_html'), banner)


def escape(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


@rt('/trace/{tid}')
def get(tid: int, download: str = ''):
    with db() as con:
        r = con.execute('SELECT * FROM traces WHERE id=?', (tid,)).fetchone()
    if not r:
        return home('That saved trace no longer exists.')
    har = json.loads(gzip.decompress(r['har']).decode('utf-8-sig'))
    log = json.loads(gzip.decompress(r['log']).decode('utf-8-sig')) if r['log'] else None
    data = timeline.build(har, log, title=r['title'], har_name=r['har_name'] or '', log_name=r['log_name'] or '',
                          action=r['action'], download=r['download'])
    return respond(timeline.render(data), r['title'], download,
                   f'<span style="color:var(--muted)">Saved {escape(_when(r["created"]))}</span>')


@rt('/trace/{tid}/delete')
def post(tid: int):
    with db() as con:
        con.execute('DELETE FROM traces WHERE id=?', (tid,))
    return RedirectResponse('/', status_code=303)


serve()
