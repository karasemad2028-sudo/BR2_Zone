#!/usr/bin/env python3
import asyncio, json, hashlib, sqlite3, random, os, time
from aiohttp import web, WSMsgType

DB_PATH = "nile.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            avatar TEXT DEFAULT '𓆣',
            color TEXT DEFAULT '#c9a84c',
            created_at REAL DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE IF NOT EXISTS friendships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, friend_id INTEGER,
            UNIQUE(user_id, friend_id)
        );
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, icon TEXT DEFAULT '𓂀',
            description TEXT DEFAULT '', created_by INTEGER,
            created_at REAL DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER, user_id INTEGER,
            content TEXT NOT NULL,
            created_at REAL DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE IF NOT EXISTS channel_members (
            channel_id INTEGER, user_id INTEGER,
            UNIQUE(channel_id, user_id)
        );
    """)
    c.execute("SELECT COUNT(*) FROM channels")
    if c.fetchone()[0] == 0:
        for row in [("general","𓂀","A place for everyone"),("ancient-arts","𓇼","Art & culture"),("river-talk","𓆣","Random")]:
            c.execute("INSERT INTO channels (name,icon,description) VALUES (?,?,?)", row)
    conn.commit(); conn.close()

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

AVATARS = ['𓁿','𓂀','𓃾','𓄿','𓅓','𓆣','𓇼','𓈖']
COLORS  = ['#c9a84c','#4a90c4','#3a7a5a','#9a6ac4','#c44a6a','#4ac4a8','#c47a4a']
REJECTIONS = ['The stars are not aligned — try again','The Nile floods unpredictably. Retry.','Your name echoes strangely in these waters','The papyrus is wet. Wait and try again.']

connected = {}  # ws -> user_id
user_ws   = {}  # user_id -> ws

async def ws_send(ws, data):
    try: await ws.send_str(json.dumps(data, ensure_ascii=False))
    except: pass

async def ws_broadcast(data, exclude=None):
    txt = json.dumps(data, ensure_ascii=False)
    for ws in list(connected.keys()):
        if ws != exclude:
            try: await ws.send_str(txt)
            except: pass

async def do_register(ws, msg):
    u,e,p = msg.get('username','').strip(), msg.get('email','').strip(), msg.get('password','')
    if not u or not e or not p:
        await ws_send(ws, {'type':'register_result','ok':False,'reason':'All fields required'}); return
    if len(p)<8:
        await ws_send(ws, {'type':'register_result','ok':False,'reason':'Password must be at least 8 characters'}); return
    if '@' not in e:
        await ws_send(ws, {'type':'register_result','ok':False,'reason':'Invalid email format'}); return
    if random.random()<0.20:
        await ws_send(ws, {'type':'register_result','ok':False,'reason':random.choice(REJECTIONS)}); return
    av,co = random.choice(AVATARS), random.choice(COLORS)
    try:
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("INSERT INTO users (username,email,password_hash,avatar,color) VALUES (?,?,?,?,?)", (u,e,hash_pw(p),av,co))
        uid = c.lastrowid
        c.execute("SELECT id FROM channels")
        for (cid,) in c.fetchall(): c.execute("INSERT OR IGNORE INTO channel_members VALUES (?,?)", (cid,uid))
        conn.commit(); conn.close()
        await ws_send(ws, {'type':'register_result','ok':True,'user_id':uid,'username':u,'avatar':av,'color':co})
    except sqlite3.IntegrityError as ex:
        msg2 = 'Username already taken' if 'username' in str(ex) else 'Email already registered'
        await ws_send(ws, {'type':'register_result','ok':False,'reason':msg2})

async def do_login(ws, msg):
    u,p = msg.get('username','').strip(), msg.get('password','')
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT id,username,avatar,color FROM users WHERE username=? AND password_hash=?", (u,hash_pw(p)))
    row = c.fetchone(); conn.close()
    if not row:
        await ws_send(ws, {'type':'login_result','ok':False,'reason':'Wrong username or password'}); return
    uid,uname,av,co = row
    connected[ws]=uid; user_ws[uid]=ws
    await ws_broadcast({'type':'user_online','user_id':uid,'username':uname}, exclude=ws)
    await ws_send(ws, {'type':'login_result','ok':True,'user_id':uid,'username':uname,'avatar':av,'color':co})

async def do_get_channels(ws):
    uid = connected.get(ws)
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT ch.id,ch.name,ch.icon,ch.description FROM channels ch JOIN channel_members cm ON cm.channel_id=ch.id WHERE cm.user_id=? ORDER BY ch.id", (uid,))
    chs = [{'id':r[0],'name':r[1],'icon':r[2],'desc':r[3]} for r in c.fetchall()]
    conn.close(); await ws_send(ws, {'type':'channels_list','channels':chs})

async def do_get_friends(ws):
    uid = connected.get(ws)
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT u.id,u.username,u.avatar,u.color FROM users u JOIN friendships f ON f.friend_id=u.id WHERE f.user_id=?", (uid,))
    online_ids = set(v for v in connected.values() if v)
    friends = [{'id':r[0],'username':r[1],'avatar':r[2],'color':r[3],'online':r[0] in online_ids} for r in c.fetchall()]
    conn.close(); await ws_send(ws, {'type':'friends_list','friends':friends})

async def do_add_friend(ws, msg):
    uid = connected.get(ws); fname = msg.get('username','').strip()
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT id FROM users WHERE username=?", (fname,))
    row = c.fetchone()
    if not row: conn.close(); await ws_send(ws, {'type':'add_friend_result','ok':False,'reason':'User not found'}); return
    fid = row[0]
    if fid==uid: conn.close(); await ws_send(ws, {'type':'add_friend_result','ok':False,'reason':'Cannot add yourself'}); return
    try:
        c.execute("INSERT INTO friendships (user_id,friend_id) VALUES (?,?)", (uid,fid))
        c.execute("INSERT OR IGNORE INTO friendships (user_id,friend_id) VALUES (?,?)", (fid,uid))
        conn.commit(); conn.close()
        await ws_send(ws, {'type':'add_friend_result','ok':True,'message':f'{fname} added!'})
    except sqlite3.IntegrityError:
        conn.close(); await ws_send(ws, {'type':'add_friend_result','ok':False,'reason':'Already friends'})

async def do_get_messages(ws, msg):
    cid = msg.get('channel_id')
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT m.id,u.username,u.avatar,u.color,m.content,m.created_at FROM messages m JOIN users u ON m.user_id=u.id WHERE m.channel_id=? ORDER BY m.created_at DESC LIMIT 80", (cid,))
    rows = list(reversed(c.fetchall())); conn.close()
    msgs = [{'id':r[0],'author':r[1],'avatar':r[2],'color':r[3],'text':r[4],'ts':r[5]} for r in rows]
    await ws_send(ws, {'type':'messages_list','channel_id':cid,'messages':msgs})

async def do_send_message(ws, msg):
    uid=connected.get(ws); cid=msg.get('channel_id'); content=msg.get('content','').strip()
    if not content or not uid: return
    conn=sqlite3.connect(DB_PATH); c=conn.cursor()
    c.execute("SELECT username,avatar,color FROM users WHERE id=?", (uid,)); u=c.fetchone()
    c.execute("INSERT INTO messages (channel_id,user_id,content) VALUES (?,?,?)", (cid,uid,content))
    mid=c.lastrowid; ts=time.time(); conn.commit(); conn.close()
    await ws_broadcast({'type':'new_message','channel_id':cid,'id':mid,'author':u[0],'avatar':u[1],'color':u[2],'text':content,'ts':ts})

async def do_create_channel(ws, msg):
    uid=connected.get(ws); name=msg.get('name','').strip().lower().replace(' ','-')
    icon=msg.get('icon','𓂀'); desc=msg.get('desc','')
    if not name: return
    conn=sqlite3.connect(DB_PATH); c=conn.cursor()
    c.execute("INSERT INTO channels (name,icon,description,created_by) VALUES (?,?,?,?)", (name,icon,desc,uid))
    cid=c.lastrowid; c.execute("INSERT INTO channel_members VALUES (?,?)", (cid,uid))
    conn.commit(); conn.close()
    await ws_send(ws, {'type':'channel_created','channel':{'id':cid,'name':name,'icon':icon,'desc':desc}})

HANDLERS = {'register':do_register,'login':do_login,'get_channels':do_get_channels,
            'get_friends':do_get_friends,'add_friend':do_add_friend,'get_messages':do_get_messages,
            'send_message':do_send_message,'create_channel':do_create_channel}

async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    connected[ws] = None
    try:
        async for raw in ws:
            if raw.type == WSMsgType.TEXT:
                try:
                    msg = json.loads(raw.data)
                    h = HANDLERS.get(msg.get('type'))
                    if h: await h(ws, msg)
                except Exception as e: print(f"Err: {e}")
            elif raw.type in (WSMsgType.ERROR, WSMsgType.CLOSE): break
    finally:
        uid = connected.pop(ws, None)
        if uid:
            user_ws.pop(uid, None)
            await ws_broadcast({'type':'user_offline','user_id':uid})
    return ws

HTML_CACHE = None

async def serve_html(request):
    return web.Response(text=HTML_CACHE, content_type='text/html', charset='utf-8')

async def main():
    global HTML_CACHE
    init_db()
    PORT = int(os.environ.get('PORT', 8080))
    DOMAIN = os.environ.get('RAILWAY_PUBLIC_DOMAIN', f'localhost:{PORT}')
    WS_URL = f"wss://{DOMAIN}/ws"

    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'nile_client.html')
    with open(html_path, 'r', encoding='utf-8') as f:
        content = f.read()
    HTML_CACHE = content.replace("'ws://localhost:8080'", f"'{WS_URL}'")

    print(f"𓆣 The Nile — http://0.0.0.0:{PORT}  |  WS: {WS_URL}")

    app = web.Application()
    app.router.add_get('/', serve_html)
    app.router.add_get('/ws', websocket_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    print(f"Server running on port {PORT}")
    await asyncio.Future()

if __name__ == '__main__':
    asyncio.run(main())
