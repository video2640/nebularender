import os
import re
import base64
import mimetypes
import secrets
from urllib.parse import quote

from aiohttp import web
import pymongo
from pyrogram import Client
from pyrogram.file_id import FileId
import pyrogram.raw.functions.upload as upload
import pyrogram.raw.types as types
import pyrogram.errors

MONGO_URI = os.environ.get('MONGO_URI', '')
TG_API_ID = int(os.environ.get('TG_API_ID', '0'))
TG_API_HASH = os.environ.get('TG_API_HASH', '')
TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN', '')
TG_CHAT_ID = int(os.environ.get('TG_CHAT_ID', '0'))
AUTH_USER = os.environ.get('AUTH_USER', 'admin')
AUTH_PASS = os.environ.get('AUTH_PASS', 'mtp123')
CHUNK_SIZE = int(os.environ.get('CHUNK_SIZE', '1048576'))

sessions = {}
_mongo_client = None
_tg_client = None


def get_mongo():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = pymongo.MongoClient(MONGO_URI)
    return _mongo_client['ftp']


async def get_tg():
    global _tg_client
    if _tg_client is None:
        _tg_client = Client(
            'render_bot',
            api_id=TG_API_ID,
            api_hash=TG_API_HASH,
            bot_token=TG_BOT_TOKEN,
            in_memory=True,
            workdir='/tmp/pyrogram'
        )
        await _tg_client.start()
        print('Telegram client started')
    return _tg_client


def check_session(request):
    sid = request.cookies.get('session')
    if sid and sid in sessions:
        return True
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Basic '):
        try:
            decoded = base64.b64decode(auth[6:]).decode('utf-8')
            u, _, p = decoded.partition(':')
            if u == AUTH_USER and p == AUTH_PASS:
                return True
        except Exception:
            pass
    return False


def parse_range(range_hdr, total):
    if not range_hdr:
        return 0, total - 1
    m = re.match(r'bytes=(\d*)-(\d*)', range_hdr)
    if not m:
        return 0, total - 1
    s = m.group(1)
    e = m.group(2)
    start = int(s) if s else 0
    end = int(e) if e else total - 1
    return start, min(end, total - 1)


async def get_part_bytes(client, tg_file, offset, limit):
    fid = FileId.decode(str(tg_file))
    loc = types.InputDocumentFileLocation(
        id=fid.media_id,
        access_hash=fid.access_hash,
        file_reference=fid.file_reference,
        thumb_size=fid.thumb_size or ''
    )
    try:
        res = await client.invoke(
            upload.GetFile(location=loc, offset=offset, limit=limit)
        )
        return res.bytes
    except pyrogram.errors.FileReferenceExpired:
        print('FileReferenceExpired, refreshing...')
        msg_id = getattr(fid, 'message_id', None)
        if TG_CHAT_ID and msg_id:
            try:
                msg = await client.get_messages(TG_CHAT_ID, msg_id)
                if msg and hasattr(msg, 'document') and msg.document:
                    loc.file_reference = msg.document.file_reference
                    res = await client.invoke(
                        upload.GetFile(location=loc, offset=offset, limit=limit)
                    )
                    return res.bytes
            except Exception as e:
                print(f'Refresh failed: {e}')
        raise


async def handle_index(request):
    raise web.HTTPFound('/browse?path=/')


async def handle_login_get(request):
    html = '''<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Login</title><style>
body{font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;background:#f0f2f5;margin:0}
form{background:#fff;padding:2rem;border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,.1);text-align:center}
input{display:block;margin:1rem auto;padding:.5rem;width:200px;border:1px solid #ccc;border-radius:4px}
button{padding:.5rem 1rem;background:#1877f2;color:#fff;border:none;border-radius:4px;cursor:pointer}
h2{margin-top:0}</style></head><body>
<form method="post"><h2>Login</h2>
<input type="text" name="user" placeholder="Username" required>
<input type="password" name="pass" placeholder="Password" required>
<button type="submit">Login</button></form></body></html>'''
    return web.Response(text=html, content_type='text/html')


async def handle_login_post(request):
    data = await request.post()
    if data.get('user') == AUTH_USER and data.get('pass') == AUTH_PASS:
        sid = secrets.token_hex(32)
        sessions[sid] = True
        resp = web.HTTPFound('/browse?path=/')
        resp.set_cookie('session', sid, path='/')
        return resp
    return web.Response(text='Invalid credentials', status=401)


async def handle_logout(request):
    sid = request.cookies.get('session')
    sessions.pop(sid, None)
    resp = web.HTTPFound('/login')
    resp.del_cookie('session')
    return resp


async def handle_browse(request):
    if not check_session(request):
        raise web.HTTPUnauthorized(headers={'WWW-Authenticate': 'Basic realm="Login"'})

    path = request.query.get('path', '/')
    if not path.startswith('/'):
        path = '/' + path
    path = path.rstrip('/') or '/'

    db = get_mongo()
    col = db['files']

    files = list(col.find({'parent': path}).sort('name', 1))

    if path == '/':
        pattern = '^/[^/]+$'
    else:
        pattern = f'^{re.escape(path)}/[^/]+$'
    parents = col.distinct('parent', {'parent': {'$regex': pattern}})
    subdirs = sorted(set(p.split('/')[-1] for p in parents))

    crumbs = path.strip('/').split('/') if path != '/' else []
    bread = '<a href="/browse?path=/">root</a>'
    acc = ''
    for crumb in crumbs:
        acc += '/' + crumb
        bread += f' / <a href="/browse?path={quote(acc)}">{crumb}</a>'

    html = '<!DOCTYPE html><html><head><meta charset="utf-8"><title>Browse</title>'
    html += '<style>body{font-family:sans-serif;margin:2rem;background:#f5f5f5;color:#333}'
    html += 'a{color:#1877f2;text-decoration:none}a:hover{text-decoration:underline}'
    html += '.entry{padding:.25rem .5rem}.size{color:#999;font-size:.85em;margin-left:.5rem}'
    html += 'hr{border:none;border-top:1px solid #ddd}'
    html += '</style></head><body>'
    html += f'<h3>{bread}</h3><hr>'

    for d in subdirs:
        p = (path + '/' + d) if path != '/' else '/' + d
        html += f'<div class="entry">&#128193; <a href="/browse?path={quote(p)}">{d}/</a></div>'

    for f in files:
        fp = path + '/' + f['name']
        sz = f.get('size', 0)
        sz_s = f'{sz/1024/1024:.1f}MB' if sz >= 1048576 else f'{sz/1024:.1f}KB' if sz >= 1024 else f'{sz}B'
        html += f'<div class="entry">&#128196; <a href="/stream?path={quote(fp)}">{f["name"]}</a>'
        html += f'<span class="size">{sz_s}</span>'
        html += f' <a href="/delete?path={quote(fp)}" style="color:#e74c3c;font-size:.8em" onclick="return confirm(\'Delete {f["name"]}?\')">[del]</a>'
        html += '</div>'

    html += '</body></html>'
    return web.Response(text=html, content_type='text/html')


async def handle_stream(request):
    filepath = request.query.get('path', '')
    if not filepath or '/' not in filepath:
        return web.Response(text='Invalid path', status=400)

    idx = filepath.rfind('/')
    parent = filepath[:idx] if idx > 0 else '/'
    name = filepath[idx + 1:]
    if not parent.startswith('/'):
        parent = '/' + parent

    db = get_mongo()
    doc = db['files'].find_one({'parent': parent, 'name': name})
    if not doc:
        return web.Response(text='File not found', status=404)

    parts = sorted(doc.get('parts', []), key=lambda p: p.get('part_id', 0))
    if not parts:
        return web.Response(text='No parts', status=404)

    total = sum(p.get('size', 0) for p in parts)
    if total == 0:
        total = len(parts) * CHUNK_SIZE

    range_hdr = request.headers.get('Range', '')
    start, end = parse_range(range_hdr, total)
    length = end - start + 1

    client = await get_tg()
    mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'

    resp = web.StreamResponse()
    resp.headers['Accept-Ranges'] = 'bytes'
    resp.headers['Content-Type'] = mime

    if range_hdr:
        resp.set_status(206)
        resp.headers['Content-Range'] = f'bytes {start}-{end}/{total}'

    await resp.prepare(request)

    for pi, part in enumerate(parts):
        part_size = part.get('size', CHUNK_SIZE)
        part_start = pi * CHUNK_SIZE
        part_end = part_start + part_size - 1

        if part_end < start:
            continue
        if part_start > end:
            break

        read_off = max(0, start - part_start)
        read_end = min(part_size - 1, end - part_start)
        read_len = read_end - read_off + 1

        try:
            data = await get_part_bytes(client, part['tg_file'], read_off, read_len)
            if data:
                await resp.write(data)
        except Exception as e:
            print(f'Part {pi} error: {e}')
            break

    await resp.write_eof()
    return resp


async def handle_thumb(request):
    filepath = request.query.get('path', '')
    if not filepath or '/' not in filepath:
        return web.Response(text='Invalid path', status=400)

    idx = filepath.rfind('/')
    parent = filepath[:idx] if idx > 0 else '/'
    name = filepath[idx + 1:]
    if not parent.startswith('/'):
        parent = '/' + parent

    db = get_mongo()
    doc = db['files'].find_one({'parent': parent, 'name': name})
    if not doc:
        return web.Response(text='File not found', status=404)

    parts = sorted(doc.get('parts', []), key=lambda p: p.get('part_id', 0))
    if not parts:
        return web.Response(text='No parts', status=404)

    limit = min(262144, parts[0].get('size', CHUNK_SIZE))
    client = await get_tg()

    try:
        data = await get_part_bytes(client, parts[0]['tg_file'], 0, limit)
    except Exception as e:
        print(f'Thumb error: {e}')
        return web.Response(text='Error', status=500)

    mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
    return web.Response(body=data, content_type=mime, headers={'Accept-Ranges': 'bytes'})


async def handle_playlist(request):
    if not check_session(request):
        raise web.HTTPUnauthorized(headers={'WWW-Authenticate': 'Basic realm="Login"'})

    path = request.query.get('path', '/')
    if not path.startswith('/'):
        path = '/' + path
    path = path.rstrip('/') or '/'

    db = get_mongo()
    files = list(db['files'].find({'parent': path}).sort('name', 1))

    lines = ['#EXTM3U']
    for f in files:
        fp = path + '/' + f['name']
        sz = f.get('size', 0)
        dur = max(1, int(sz / (1024 * 1024)))
        lines.append(f'#EXTINF:{dur},{f["name"]}')
        lines.append(f'/stream?path={quote(fp)}')

    return web.Response(text='\n'.join(lines) + '\n', content_type='audio/x-mpegurl')


async def handle_delete(request):
    if not check_session(request):
        raise web.HTTPUnauthorized(headers={'WWW-Authenticate': 'Basic realm="Login"'})

    filepath = request.query.get('path', '')
    if not filepath or '/' not in filepath:
        return web.Response(text='Invalid path', status=400)

    idx = filepath.rfind('/')
    parent = filepath[:idx] if idx > 0 else '/'
    name = filepath[idx + 1:]
    if not parent.startswith('/'):
        parent = '/' + parent

    db = get_mongo()
    result = db['files'].delete_one({'parent': parent, 'name': name})

    if result.deleted_count == 0:
        return web.Response(text='File not found', status=404)

    raise web.HTTPFound(f'/browse?path={quote(parent)}')


app = web.Application()
app.router.add_get('/', handle_index)
app.router.add_get('/login', handle_login_get)
app.router.add_post('/login', handle_login_post)
app.router.add_get('/logout', handle_logout)
app.router.add_get('/browse', handle_browse)
app.router.add_get('/stream', handle_stream)
app.router.add_get('/thumb', handle_thumb)
app.router.add_get('/playlist.m3u', handle_playlist)
app.router.add_get('/delete', handle_delete)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print(f'Starting on 0.0.0.0:{port}')
    web.run_app(app, host='0.0.0.0', port=port, access_log=None)
