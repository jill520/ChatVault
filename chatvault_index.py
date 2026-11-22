#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chatvault_index.py - 归档全文索引（SQLite FTS5）

为什么需要它：
  原来的搜索每次把 500MB 的 Markdown 全读一遍再按行 grep，结果是
  "文件名字母序 + 60 条硬截断"，既慢又无序，无法分页/排序/按会话归组。

现在的做法：
  一次建索引 → .local/index.db（增量：按文件 mtime+size 判重，只重解析变化的文件）。
  中文用"逐字切分 + 短语查询"进 FTS5，任意长度的中文子串都能精确命中。

对外接口：
  index_platform(platform, log)      建/增量更新索引
  search(...)                        结构化检索（含总数、分页、会话聚合）
  conv_days(conv, platform)          某会话有哪些日期（阅读器用）
  window(conv, platform, ...)        取某会话某日/某锚点附近的消息（阅读器用）
  convs(platform)                    会话清单（选择器用）
  stats()                            索引概况

只读归档，索引库写在 .local/ 下，删掉可随时重建。
"""
import os
import re
import json
import glob
import sqlite3
import zlib
import threading
from datetime import datetime

import archive_parse as ap
import local_config as cfg

_LOCK = threading.Lock()
_LOCAL = threading.local()
_RESOLVER = [None]
_BUILDING = {"running": False, "platform": "", "done": 0, "total": 0, "msg": 0, "text": ""}


def db_path():
    return cfg.index_db()


def _conn():
    c = getattr(_LOCAL, "conn", None)
    if c is not None:
        return c
    os.makedirs(os.path.dirname(db_path()) or cfg.LOCAL, exist_ok=True)
    c = sqlite3.connect(db_path(), timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA temp_store=MEMORY")
    c.execute("PRAGMA cache_size=-65536")
    c.create_function("REGEXP", 2, _regexp)
    _init_schema(c)
    _LOCAL.conn = c
    return c


def _regexp(pattern, value):
    try:
        return 1 if re.search(pattern, value or "", re.I) else 0
    except re.error:
        return 0


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS files(
        path TEXT PRIMARY KEY, mtime REAL, size INTEGER, n INTEGER,
        platform TEXT, conv TEXT, indexed_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS msg(
        id INTEGER PRIMARY KEY, fid INTEGER, platform TEXT, kind TEXT, src TEXT,
        conv TEXT, conv_name TEXT, conv_qq TEXT,
        date TEXT, time TEXT, ts INTEGER,
        sender TEXT, qq TEXT, is_self INTEGER, reply TEXT, text TEXT)""",
    "CREATE INDEX IF NOT EXISTS ix_msg_conv ON msg(conv, ts, id)",
    "CREATE INDEX IF NOT EXISTS ix_msg_pc ON msg(platform, conv, ts, id)",
    "CREATE INDEX IF NOT EXISTS ix_msg_ts ON msg(platform, ts)",
    "CREATE INDEX IF NOT EXISTS ix_msg_fid ON msg(fid)",
    """CREATE TABLE IF NOT EXISTS conv_stats(
        platform TEXT, conv TEXT, conv_name TEXT, conv_qq TEXT,
        n INTEGER, a INTEGER, b INTEGER, me INTEGER,
        PRIMARY KEY(platform, conv))""",
    "CREATE VIRTUAL TABLE IF NOT EXISTS msg_fts USING fts5(seg)",
]


def _init_schema(c):
    for sql in SCHEMA:
        c.execute(sql)
    c.commit()


def resolver():
    if _RESOLVER[0] is None:
        _RESOLVER[0] = ap.Resolver(cfg.BASE)
    return _RESOLVER[0]


def reset_resolver():
    _RESOLVER[0] = None


# ---------------------------------------------------------------- 建索引

def scan_roots(platform):
    """检索范围：归档根 + 增量目录（今天的新消息在增量里）。"""
    out = []
    r = cfg.roots().get(platform)
    if r and os.path.isdir(r):
        out.append((r, "archive"))
    incr = cfg.qq_incr() if platform == "QQ" else cfg.wechat_incr()
    if incr and os.path.isdir(incr) and incr != r:
        out.append((incr, "incr"))
    return out


def _files_of(platform):
    out = []
    for root, src in scan_roots(platform):
        for f in glob.glob(os.path.join(root, "**", "*.md"), recursive=True):
            if os.path.basename(f).upper() == "INDEX.MD":
                continue
            try:
                st = os.stat(f)
            except OSError:
                continue
            out.append((f, root, src, st.st_mtime, st.st_size))
    out.sort()
    return out


def index_platform(platform, log=None, force=False):
    """增量重建某平台的索引。返回统计字典。"""
    log = log or (lambda *_: None)
    c = _conn()
    R = resolver()
    files = _files_of(platform)
    # 先用目录名把映射表缺口补上（否则 118 个私聊会话只有裸 QQ 号）
    try:
        R.learn_from_dirs({root for (_f, root, _s, _m, _z) in files})
    except Exception:
        pass
    known = {r["path"]: (r["mtime"], r["size"]) for r in
             c.execute("SELECT path,mtime,size FROM files WHERE platform=?", (platform,))}
    todo = [(f, root, src, mt, sz) for (f, root, src, mt, sz) in files
            if force or known.get(f) != (mt, sz)]
    if force:
        with _LOCK:
            c.execute("DELETE FROM msg_fts WHERE rowid IN (SELECT id FROM msg WHERE platform=?)", (platform,))
            c.execute("DELETE FROM msg WHERE platform=?", (platform,))
            c.execute("DELETE FROM files WHERE platform=?", (platform,))
            c.commit()
        known = {}
    _BUILDING.update(running=True, platform=platform, done=0, total=len(todo), msg=0, text="准备中…")
    log("待解析文件 %d 个（未变化 %d 个跳过）" % (len(todo), len(files) - len(todo)))

    nxt = c.execute("SELECT COALESCE(MAX(id),0) FROM msg").fetchone()[0]
    total_msg = 0
    for i, (f, root, src, mt, sz) in enumerate(todo, 1):
        if os.environ.get("CHATVAULT_INDEX_STOP") == "1":
            break
        kind_c, conv, conv_name, conv_qq = R.conv(f, root)
        # 必须用稳定哈希：Python 的 hash() 每个进程加盐不同，
        # 换进程再解析同一文件会算出不同 fid，导致删不掉旧行、索引里出现重复消息
        fid = zlib.crc32(f.encode("utf-8", "replace"))
        recs = ap.parse_file(f)
        rows, segs = [], []
        for r in recs:
            nxt += 1
            nm, qq, mine = R.sender(r["sender_raw"])
            k = ap.classify(r["text"])
            if k == "system" or not nm or nm == "未知":
                if k == "system":
                    nm = "系统"
            txt = r["text"]
            ts = ap.ts_of(r["date"], r["time"])
            # 卡片/富文本/超长消息只存短占位：正文是原始 XML/JSON，存全量只会把库撑爆，
            # 而且检索它们没有意义（展示层本来就折叠成 [卡片]/[富文本]）
            if k in ("json", "card", "huge"):
                # 卡片/富文本压成一句人话再入库：原文是 XML/JSON，
                # 存全量会把库撑爆，存残片又什么都看不出来
                store = ap.summary(txt, k)[:300]
                seg = " ".join(ap.segment(store)) if k != "huge" else ""
            else:
                store = txt[:4000]
                # 引用原文也要能被搜到（QQ 回复把被引用的那句挪进了 reply 字段）
                seg = " ".join(ap.segment(((r.get("reply") or "") + " " + txt)[:2000]))
            rows.append((nxt, fid, platform, k, src, conv, conv_name, conv_qq,
                         r["date"], r["time"], ts, nm, qq, 1 if mine else 0,
                         r.get("reply", ""), store))
            if seg:
                segs.append((nxt, seg))
        with _LOCK:
            c.execute("DELETE FROM msg_fts WHERE rowid IN (SELECT id FROM msg WHERE fid=?)", (fid,))
            c.execute("DELETE FROM msg WHERE fid=?", (fid,))
            if rows:
                c.executemany(
                    "INSERT INTO msg(id,fid,platform,kind,src,conv,conv_name,conv_qq,"
                    "date,time,ts,sender,qq,is_self,reply,text) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                c.executemany("INSERT INTO msg_fts(rowid,seg) VALUES (?,?)", segs)
            c.execute("INSERT OR REPLACE INTO files(path,mtime,size,n,platform,conv,indexed_at) "
                      "VALUES (?,?,?,?,?,?,?)",
                      (f, mt, sz, len(rows), platform, conv,
                       datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            if i % 30 == 0 or i == len(todo):
                c.commit()
        total_msg += len(rows)
        _BUILDING.update(done=i, msg=total_msg)
        if i % 50 == 0 or i == len(todo):
            log("已解析 %d/%d 个文件，新增/更新 %d 条消息" % (i, len(todo), total_msg))
    _BUILDING.update(running=False, text="完成")
    with _LOCK:
        try:
            # 大批量写入后把 WAL 收回主库，否则 -wal 文件会长期占几百 MB
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
    n_all = c.execute("SELECT COUNT(*) FROM msg WHERE platform=?", (platform,)).fetchone()[0]
    try:
        has_stats = c.execute("SELECT COUNT(*) FROM conv_stats WHERE platform=?",
                              (platform,)).fetchone()[0]
    except sqlite3.Error:
        has_stats = 0
    if todo or not has_stats:
        _rebuild_conv_stats(platform)
    log("索引完成：%s 共 %d 条消息" % (platform, n_all))
    return {"平台": platform, "文件": len(files), "更新文件": len(todo),
            "消息": n_all, "本次写入": total_msg}


def build_progress():
    return dict(_BUILDING)


def stats():
    c = _conn()
    out = {"平台": {}, "库大小MB": 0, "索引时间": ""}
    for row in c.execute("SELECT platform, COUNT(*) n, COUNT(DISTINCT conv) k FROM msg GROUP BY platform"):
        out["平台"][row["platform"]] = {"消息": row["n"], "会话": row["k"]}
    try:
        out["库大小MB"] = round(os.path.getsize(db_path()) / 1048576, 1)
        out["索引时间"] = datetime.fromtimestamp(os.path.getmtime(db_path())).strftime("%Y-%m-%d %H:%M")
    except OSError:
        pass
    return out


# ---------------------------------------------------------------- 查询

DEFAULT_KINDS = ("text", "media", "card", "system", "huge")


def _where(platform, conv, date_from, date_to, kinds, sender, include_all):
    w = ["m.platform = ?"]
    p = [platform]
    if conv:
        w.append("m.conv = ?")
        p.append(conv)
    if date_from:
        lo = ap.ts_of(date_from, "00:00:00")
        # 微信缺日的记录是 YYYY-MM-00。用户选"从某月 1 日"时，
        # 这些只知月份、不知哪天的记录也要算进这个月，否则会凭空消失。
        try:
            if str(date_from)[8:10] == "01":
                lo = int("%04d%02d00000000" % (int(date_from[:4]), int(date_from[5:7])))
        except (TypeError, ValueError):
            pass
        w.append("m.ts >= ?")
        p.append(lo)
    if date_to:
        w.append("m.ts <= ?")
        p.append(ap.ts_of(date_to, "23:59:59"))
    if sender:
        # 昵称片段或精确 QQ 号都能筛（群聊里同一个人可能既有昵称标签又有 QQ 标签）
        w.append("(m.sender LIKE ? OR m.qq = ?)")
        p.extend(["%" + sender + "%", sender])
    if not include_all:
        w.append("m.kind IN (%s)" % ",".join("?" * len(kinds)))
        p.extend(kinds)
    return " AND ".join(w), p


def _row(r):
    return {"id": r["id"], "平台": r["platform"], "会话": r["conv"], "会话名": r["conv_name"],
            "会话号": r["conv_qq"], "日期": r["date"], "时间": r["time"], "ts": r["ts"],
            "发送者": r["sender"], "qq": r["qq"], "我": bool(r["is_self"]),
            "类型": r["kind"], "引用": r["reply"], "来源": r["src"],
            "正文": r["text"], "显示": ap.pretty_text(r["text"], r["kind"])}


def search(platform, q, use_regex=False, conv="", date_from="", date_to="",
           sender="", include_all=False, offset=0, limit=50):
    """结构化检索。返回 {总数, 上限?, 用时, 结果[], 会话聚合[]}。"""
    c = _conn()
    t0 = datetime.now()
    kinds = DEFAULT_KINDS
    where, params = _where(platform, conv, date_from, date_to, kinds, sender, include_all)
    q = (q or "").strip()
    if not q and not (conv or sender or date_from or date_to):
        # 没有关键词也没有任何范围条件：不让它变成"列出全库 220 万条"
        return {"结果": [], "总数": 0, "用时": 0}

    if not q:
        # 浏览模式：选了一个会话（或一个人），不需要关键词，按时间倒序列出
        base = "FROM msg m WHERE %s" % where
        params2 = list(params)
        order = "ORDER BY m.ts DESC, m.id DESC"
    elif use_regex:
        try:
            re.compile(q)
        except re.error as e:
            return {"错误": "正则错误: %s" % e, "结果": [], "总数": 0}
        base = "FROM msg m WHERE %s AND m.text REGEXP ?" % where
        params2 = params + [q]
        order = "ORDER BY m.ts DESC, m.id DESC"
    else:
        fq = ap.fts_query(q)
        if not fq:
            return {"结果": [], "总数": 0, "用时": 0}
        base = ("FROM msg_fts f JOIN msg m ON m.id = f.rowid WHERE msg_fts MATCH ? AND %s"
                % where)
        params2 = [fq] + params
        order = "ORDER BY m.ts DESC, m.id DESC"

    # 总数：封顶 20000，避免超高频词把 COUNT 拖死
    try:
        cnt = c.execute("SELECT COUNT(*) FROM (SELECT 1 %s LIMIT 20001)" % base,
                        params2).fetchone()[0]
    except sqlite3.Error as e:
        return {"错误": str(e), "结果": [], "总数": 0}
    capped = cnt > 20000

    rows = c.execute("SELECT m.* %s %s LIMIT ? OFFSET ?" % (base, order),
                     params2 + [limit, offset]).fetchall()
    # 归档里存在真重复（同会话/同时间/同人/同文），同步时被写了两次。
    # 只在本页内去重，不动总数——为了几十条重复去跑全表自连接不值得。
    rows = _dedup(rows)

    # 会话聚合（判断"这句话属于哪几场对话"）
    groups = []
    if cnt and cnt <= 20000:
        try:
            groups = [dict(会话=r["conv"], 会话名=r["conv_name"], 条数=r["n"], 最后=r["mx"])
                      for r in c.execute(
                          "SELECT m.conv, m.conv_name, COUNT(*) n, MAX(m.ts) mx %s "
                          "GROUP BY m.conv ORDER BY n DESC LIMIT 40" % base, params2)]
        except sqlite3.Error:
            groups = []

    return {"结果": [_row(r) for r in rows], "总数": cnt, "封顶": capped,
            "会话聚合": groups, "正则": use_regex,
            "用时": round((datetime.now() - t0).total_seconds(), 3)}


_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _stamp():
    try:
        st = os.stat(db_path())
        return (st.st_mtime, st.st_size)
    except OSError:
        return (0, 0)


def _cached(key, fn):
    stamp = _stamp()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
    val = fn()
    with _CACHE_LOCK:
        _CACHE[key] = (stamp, val)
    return val


def calendar_month(platform, year, month):
    """某个月里每天有多少条消息（时间表用）。"""
    return _cached(("cal1", platform, int(year), int(month)),
                   lambda: _calendar_month_uncached(platform, int(year), int(month)))


def _calendar_month_uncached(platform, year, month):
    if not (1 <= month <= 12):
        return {"年": year, "月": month, "天数": {}, "未知": 0}
    # 微信缺日的记录写成 YYYY-MM-00，ts 的"日"也是 00，
    # 所以下界必须从 00 日开始，否则整月这些记录会被漏掉。
    lo = int("%04d%02d00000000" % (year, month))
    hi = int("%04d%02d31235959" % (year, month))
    prefix = "%04d-%02d-" % (year, month)
    days, unknown = {}, 0
    c = _conn()
    for r in c.execute(
            "SELECT date, COUNT(*) n FROM msg "
            "WHERE platform=? AND ts>=? AND ts<=? GROUP BY date",
            (platform, lo, hi)):
        key = r["date"] or ""
        if not key.startswith(prefix):
            continue
        if key[8:10] == "00":
            unknown += r["n"]
        else:
            days[key] = r["n"]
    return {"年": year, "月": month, "天数": days, "未知": unknown}


def ts_range(platform):
    """索引里最早/最晚的消息时间，时间表用来确定能翻到哪个月。"""
    return _cached(("range1", platform), lambda: _ts_range_uncached(platform))


def _ts_range_uncached(platform):
    c = _conn()
    r = c.execute("SELECT MIN(ts) mn, MAX(ts) mx FROM msg "
                  "WHERE platform=? AND ts>0", (platform,)).fetchone()
    return {"最早": r["mn"] or 0, "最晚": r["mx"] or 0}


def _rebuild_conv_stats(platform):
    """把会话汇总单独物化成小表，避免每次打开会话页都扫 180 万条消息。"""
    c = _conn()
    rows = c.execute(
        "SELECT conv, conv_name, conv_qq, COUNT(*) n, MIN(ts) a, MAX(ts) b, "
        "SUM(is_self) me FROM msg WHERE platform=? GROUP BY conv", (platform,)).fetchall()
    data = [(platform, r["conv"], r["conv_name"], r["conv_qq"], r["n"],
             r["a"], r["b"], r["me"] or 0) for r in rows]
    with _LOCK:
        c.execute("DELETE FROM conv_stats WHERE platform=?", (platform,))
        c.executemany(
            "INSERT INTO conv_stats(platform,conv,conv_name,conv_qq,n,a,b,me) "
            "VALUES (?,?,?,?,?,?,?,?)", data)
        c.commit()
    return len(data)


def convs(platform):
    """会话清单：直接读预聚合小表，按最近活跃排序。"""
    return _cached(("convs3", platform), lambda: _convs_uncached(platform))


def _convs_uncached(platform):
    c = _conn()
    rows = c.execute(
        "SELECT conv, conv_name, conv_qq, n, a, b, me FROM conv_stats "
        "WHERE platform=? ORDER BY b DESC", (platform,)).fetchall()
    if not rows:
        # 请求路径绝不等待写锁：先用只读聚合兜底，后台再补小表
        rows = c.execute(
            "SELECT conv, conv_name, conv_qq, COUNT(*) n, MIN(ts) a, MAX(ts) b, "
            "SUM(is_self) me FROM msg WHERE platform=? GROUP BY conv ORDER BY b DESC",
            (platform,)).fetchall()

        def _bg():
            try:
                _rebuild_conv_stats(platform)
            except Exception:
                pass
        threading.Thread(target=_bg, daemon=True).start()
    return [{"会话": r["conv"], "会话名": r["conv_name"], "会话号": r["conv_qq"],
             "条数": r["n"], "起": r["a"], "止": r["b"], "我发": r["me"] or 0,
             "群": "/群聊/" in ("/" + r["conv"] + "/")} for r in rows]


def conv_days(platform, conv):
    """某会话有消息的日期列表（阅读器用来做"上一天/下一天"，带缓存）。"""
    return _cached(("days", platform, conv), lambda: _conv_days_uncached(platform, conv))


def _conv_days_uncached(platform, conv):
    c = _conn()
    return [{"日期": r["date"], "条数": r["n"]} for r in c.execute(
        "SELECT date, COUNT(*) n FROM msg WHERE platform=? AND conv=? "
        "GROUP BY date ORDER BY date", (platform, conv))]


def _order_by(platform, conv):
    """微信归档没有"日"，只能按写入顺序（id）排；QQ 按真实时间戳排。"""
    key = ("order", platform, conv)
    def calc():
        c = _conn()
        n = c.execute("SELECT COUNT(*) FROM msg WHERE platform=? AND conv=? "
                      "AND substr(date,9,2)!='00'", (platform, conv)).fetchone()[0]
        return "ts" if n else "id"
    return _cached(key, calc)


def window(platform, conv, date="", anchor_id=0, limit=400):
    """取某会话某天的全部消息；太多或没指定日期时，取锚点前后各 limit/2 条。"""
    c = _conn()
    ob = _order_by(platform, conv)
    order = "ORDER BY %s, id" % ob if ob == "ts" else "ORDER BY id"
    if date:
        rows = c.execute(
            "SELECT * FROM msg WHERE platform=? AND conv=? AND date=? %s LIMIT ?" % order,
            (platform, conv, date, limit + 1)).fetchall()
        if 0 < len(rows) <= limit:
            return [_row(r) for r in _dedup(rows)], "day", date
        if rows:
            # 当天消息过多：给出当天开头的一段，并在头部提示已截取
            return [_row(r) for r in _dedup(rows)[:limit]], "day_cut", date
    if anchor_id:
        lo = c.execute("SELECT COUNT(*) FROM msg WHERE platform=? AND conv=? AND id<?",
                       (platform, conv, anchor_id)).fetchone()[0]
        off = max(0, lo - limit // 2)
        rows = c.execute("SELECT * FROM msg WHERE platform=? AND conv=? %s LIMIT ? OFFSET ?" % order,
                         (platform, conv, limit, off)).fetchall()
        d = rows[len(rows) // 2]["date"] if rows else date
        return [_row(r) for r in _dedup(rows)], "window", d
    rows = c.execute("SELECT * FROM msg WHERE platform=? AND conv=? %s LIMIT ?" % order,
                     (platform, conv, limit)).fetchall()
    return [_row(r) for r in _dedup(rows)], "head", (rows[0]["date"] if rows else "")


def iter_conv(platform, conv, date_from="", date_to=""):
    """按归档顺序逐条产出某会话的全部消息（导出用，不整表载入内存）。"""
    c = _conn()
    ob = _order_by(platform, conv)
    order = "ORDER BY %s, id" % ob if ob == "ts" else "ORDER BY id"
    where = "platform=? AND conv=?"
    p = [platform, conv]
    if date_from:
        where += " AND date>=?"
        p.append(date_from)
    if date_to:
        where += " AND date<=?"
        p.append(date_to)
    cur = c.execute("SELECT * FROM msg WHERE %s %s" % (where, order), p)
    while True:
        rows = cur.fetchmany(1000)
        if not rows:
            break
        for r in rows:
            yield _row(r)


def _dedup(rows):
    """按 (会话,日期,时间,发送者,正文) 去掉重复行，保留第一条。"""
    out, seen = [], set()
    for r in rows:
        k = (r["conv"], r["date"], r["time"], r["sender"], r["text"])
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def senders(platform, conv="", limit=300):
    """某会话（或全平台）出现过的发送者清单，带条数。

    群聊消息里拿不到发送者 QQ 号，只能靠昵称；有了这份清单，前端就能
    在下拉里直接选"某人在这群里说过多少条"，而不是让人凭记忆敲昵称。
    """
    c = _conn()
    if conv:
        rows = c.execute(
            "SELECT sender, qq, COUNT(*) n, MAX(ts) mx FROM msg "
            "WHERE platform=? AND conv=? GROUP BY sender, qq ORDER BY n DESC LIMIT ?",
            (platform, conv, limit)).fetchall()
    else:
        rows = c.execute(
            "SELECT sender, qq, COUNT(*) n, MAX(ts) mx FROM msg "
            "WHERE platform=? GROUP BY sender, qq ORDER BY n DESC LIMIT ?",
            (platform, limit)).fetchall()
    return [{"发送者": r["sender"], "QQ": r["qq"], "条数": r["n"], "最后": r["mx"]}
            for r in rows if r["sender"]]


def count(platform, conv=""):
    c = _conn()
    if conv:
        return c.execute("SELECT COUNT(*) FROM msg WHERE platform=? AND conv=?",
                         (platform, conv)).fetchone()[0]
    return c.execute("SELECT COUNT(*) FROM msg WHERE platform=?", (platform,)).fetchone()[0]


def reset_conn():
    """索引库路径变更后调用：关掉本线程旧连接并清缓存，下次访问自动连新库。"""
    c = getattr(_LOCAL, "conn", None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
        _LOCAL.conn = None
    _CACHE.clear()


def purge_conv(platform, conv):
    """删掉某会话在索引里的全部消息与文件行。

    归档目录被移走后，索引里的旧行不会自动消失（增量只处理仍存在的文件），
    所以删除会话必须同步清索引，否则搜索还会命中"已经不存在"的群。
    顺序不能反：先按 msg 子查询删 FTS，再删 msg，最后删 files。
    """
    c = _conn()
    with _LOCK:
        n = c.execute("SELECT COUNT(*) FROM msg WHERE platform=? AND conv=?",
                      (platform, conv)).fetchone()[0]
        c.execute("DELETE FROM msg_fts WHERE rowid IN "
                  "(SELECT id FROM msg WHERE platform=? AND conv=?)", (platform, conv))
        c.execute("DELETE FROM msg WHERE platform=? AND conv=?", (platform, conv))
        f = c.execute("DELETE FROM files WHERE platform=? AND conv=?",
                      (platform, conv)).rowcount
        c.commit()
        try:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
    _CACHE.clear()
    return {"消息": n, "文件": f}


def is_empty():
    try:
        c = _conn()
        return c.execute("SELECT COUNT(*) FROM msg").fetchone()[0] == 0
    except sqlite3.Error:
        return True
