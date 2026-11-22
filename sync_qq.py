# -*- coding: utf-8 -*-
"""
QQ 增量同步管道（纯文件 · 不碰进程内存 · 不删任何文件）

流程（每次运行）：
  1. 快照复制 QQ 活跃库 nt_msg.db + -wal + -shm 到临时目录
  2. 用一次性提取的 qq_pc_key.txt 解密主库（纯 CPU，HMAC 校验）
  3. 解密 WAL 帧并按页号合并进明文库（新消息常驻 WAL）
  4. 解析 c2c_msg_table / group_msg_table → 结构化消息
  5. 用 40001(msg_id) 全局去重（state 持久化）
  6. 新消息按「会话/月份」追加写入增量归档（只追加，绝不删改历史）

红线：
  - 不删除任何已有文件；增量目录只 append；state 只覆写自身
  - 不读取 QQ 进程内存（密钥一次性提取，长期复用）
  - 密钥失效 → HMAC/校验失败 → 写 ERROR 日志退出，等重抓 key

用法：
  python sync_qq.py                 # 正常增量同步
  python sync_qq.py --init-baseline # 首次建立水位基线（不写归档）
  python sync_qq.py --dry-run       # 只探测不写入
"""
import os, sys, json, shutil, sqlite3, argparse, tempfile, time, struct, re
from datetime import datetime, timezone, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import group_alias
import user_alias
import conv_exclude

# 路径优先读本地配置（.local/config.json）；回退值为历史部署的硬编码路径
BASE = os.path.dirname(os.path.abspath(__file__))
SRC_LIB = ""  # 在 .local/config.json 配置 paths.qq_lib
KEYFILE = os.path.join(BASE, "qq_pc_key.txt")
INCR_OUT = ""  # 在 .local/config.json 配置 paths.qq_incr
STATE = os.path.join(BASE, "qq_sync_state.json")
UID_MAP = os.path.join(BASE, "uid_map.json")
LOG = os.path.join(BASE, "qq_sync.log")
try:
    import local_config as _lcfg
    BASE = _lcfg.APP_DIR
    if _lcfg.qq_lib():
        SRC_LIB = os.path.dirname(_lcfg.qq_lib())
    if _lcfg.qq_key_file():
        KEYFILE = _lcfg.qq_key_file()
    if _lcfg.qq_incr():
        INCR_OUT = _lcfg.qq_incr()
    if _lcfg.qq_log():
        STATE = os.path.join(os.path.dirname(_lcfg.qq_log()), "qq_sync_state.json")
        LOG = _lcfg.qq_log()
except Exception:
    pass
CST = timezone(timedelta(hours=8))
BIGBANG = 1600000000  # 2020-09 之前视为异常

# qq-export-helper 源码目录：优先本目录，其次打包资源目录（exe 内置）
_SRC_CANDS = [os.path.join(BASE, "tools", "qq-export-helper", "src")]
try:
    _SRC_CANDS.append(os.path.join(_lcfg.RES_DIR, "tools", "qq-export-helper", "src"))
except Exception:
    pass
SRC = next((p for p in _SRC_CANDS if os.path.isdir(p)), _SRC_CANDS[0])
sys.path.insert(0, SRC)
from qqexport import sqlcipher_py as sp
from qqexport import decrypt as qdec

def log(msg):
    line = f"[{datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")

PROGRESS_TAG = "@@PROGRESS@@ "

def progress(pct, stage):
    """工作台用它画进度条；不写入日志文件。"""
    print(f"{PROGRESS_TAG}{max(0, min(100, int(pct)))}|{stage}", flush=True)

def load_state():
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8") as f: return json.load(f)
        except Exception: pass
    return {"seen": [], "last_run": None, "total_synced": 0}

def save_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE)

SNAP_ROOT = (os.path.join(os.path.dirname(INCR_OUT), ".cache") if INCR_OUT
             else os.path.join(BASE, ".cache"))


_MUTEX_HANDLE = None


def acquire_sync_lock(name):
    """Windows 命名互斥：同一时间只允许一个同类同步进程。"""
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    try:
        import ctypes
        h = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\" + name)
        if ctypes.windll.kernel32.GetLastError() == 183:
            return False
        _MUTEX_HANDLE = h
        return True
    except Exception:
        return True


def clean_stale_snapshots(max_age=7200):
    """只清理超过 2 小时未更新的快照，避免误删另一个正在运行的同步。"""
    try:
        os.makedirs(SNAP_ROOT, exist_ok=True)
        now = time.time()
        for d in os.listdir(SNAP_ROOT):
            p = os.path.join(SNAP_ROOT, d)
            if not d.startswith("qqsnap-"):
                continue
            try:
                if now - os.path.getmtime(p) < max_age:
                    continue
            except OSError:
                continue
            shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


def snapshot():
    """快照复制 nt_msg.db + wal + shm 到 F 盘独立临时目录。"""
    os.makedirs(SNAP_ROOT, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="qqsnap-", dir=SNAP_ROOT)
    copied = 0
    for suf in ("", "-wal", "-shm"):
        s = os.path.join(SRC_LIB, "nt_msg.db" + suf)
        if os.path.exists(s):
            try:
                shutil.copy2(s, os.path.join(tmp, "nt_msg.db" + suf))
                copied += 1
            except OSError as e:
                log(f"  [WARN] 复制失败 {s}: {e}")
    return tmp, copied

def _wal_checksum(data, s0, s1, big_endian):
    """SQLite WAL 校验和：对 8 字节字累加，32 位回绕。"""
    fmt = ">II" if big_endian else "<II"
    n = len(data) // 8 * 8
    for i in range(0, n, 8):
        x, y = struct.unpack_from(fmt, data, i)
        s0 = (s0 + x + s1) & 0xFFFFFFFF
        s1 = (s1 + y + s0) & 0xFFFFFFFF
    return s0, s1

def decrypt_and_merge(snap_dir):
    """解密主库 + 构造明文 WAL 让 SQLite 自动回放并 checkpoint。"""
    src = os.path.join(snap_dir, "nt_msg.db")
    plain = os.path.join(snap_dir, "nt_msg_plain.db")
    key = open(KEYFILE, "rb").read()
    qdec.decrypt_database(src, key, plain, progress=lambda m: None)

    wal = src + "-wal"
    if not (os.path.exists(wal) and os.path.getsize(wal) > 32):
        return plain
    f = sp.get_file(src, 1024, 4096)
    if not f.check(key, deep=True):
        log("[FATAL] 密钥失效：主库 HMAC 校验失败，请重抓 key"); return None
    salt = f.salt; p = sp.DEFAULT_PARAMS
    k, hk = sp.derive_keys(key, salt, p)
    d = open(wal, "rb").read()
    magic, ver, psz, ckpt, s1w, s2w, c1w, c2w = struct.unpack(">8I", d[:32])
    big_endian = (magic == 0x377F0682)
    if magic not in (0x377F0682, 0x377F0683) or psz != 4096:
        log(f"[WARN] 未知 WAL 格式，跳过合并"); return plain

    fsz = psz + 24
    nframes = (len(d) - 32) // fsz
    # 明文 WAL：头 32 字节（magic/ver/psz/ckpt/salt1/salt2 保留，checksum 重算）
    out = bytearray(d[:32])
    c0a, c0b = s1w, s2w
    c0a, c0b = _wal_checksum(d[:24], c0a, c0b, big_endian)
    out[24:32] = struct.pack(">II", c0a, c0b)
    applied = 0
    coherent = True
    for fi in range(nframes):
        off = 32 + fi * fsz
        pgno, dbsize = struct.unpack(">II", d[off:off+8])
        enc = d[off+24:off+24+psz]
        if not any(enc):
            plainpage = b"\x00" * psz
        else:
            try:
                plainpage = sp._decrypt_page(enc, k, pgno, psz, p)
            except Exception:
                coherent = False; break
        frame = bytearray(24 + psz)
        frame[0:8] = d[off:off+8]
        frame[8:24] = d[off+8:off+24]  # salt1/salt2/checksum 占位，稍后重算
        frame[24:24+psz] = plainpage
        # 重算该帧 checksum：以头部累计值为初值，对 帧头前8 + 明文页
        newa, newb = _wal_checksum(frame[0:8], c0a, c0b, big_endian)
        newa, newb = _wal_checksum(plainpage, newa, newb, big_endian)
        frame[16:24] = struct.pack(">II", newa, newb)
        out += frame
        c0a, c0b = newa, newb
        applied += 1
    if coherent:
        pwal = plain + "-wal"
        with open(pwal, "wb") as wf: wf.write(out)
        # 让 SQLite 打开时回放 WAL，再 checkpoint 回主库
        con = sqlite3.connect(plain)
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as e:
            log(f"  [WARN] checkpoint: {e}")
        con.close()
        log(f"  WAL 回放: {applied}/{nframes} 帧 → checkpoint 完成")
    else:
        log(f"[WARN] WAL 解密中途失败({applied}/{nframes})，仅用主库数据")
    return plain

# ---------- protobuf 解析（复用 export_archive.py 逻辑）----------
def read_varint(b, i):
    r, s = 0, 0
    while True:
        if i >= len(b): raise ValueError
        x = b[i]; i += 1
        r |= (x & 0x7f) << s
        if not x & 0x80: return r, i
        s += 7
        if s > 63: raise ValueError

def pb_fields(b):
    out, i = [], 0
    while i < len(b):
        tag, i = read_varint(b, i)
        f, w = tag >> 3, tag & 7
        if w == 0: v, i = read_varint(b, i)
        elif w == 2:
            ln, i = read_varint(b, i)
            if i + ln > len(b): raise ValueError
            v = b[i:i+ln]; i += ln
        elif w == 5: v = b[i:i+4]; i += 4
        elif w == 1: v = b[i:i+8]; i += 8
        else: raise ValueError
        out.append((f, w, v))
    return out

def utf8(b):
    try:
        s = b.decode("utf-8")
        return s if all(c.isprintable() or c in "\n\r\t" for c in s) else None
    except (UnicodeDecodeError, ValueError): return None

def _json_text(s):
    """从系统灰条/卡片 JSON 里抽人话；不是 JSON 就原样返回。"""
    s = (s or "").strip()
    if not s or s[0] not in "[{":
        return s
    try:
        obj = json.loads(s)
    except Exception:
        return s
    out = []
    if isinstance(obj, dict):
        for k in ("prompt", "desc", "meta", "summary", "title"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
        for it in (obj.get("items") or []):
            if isinstance(it, dict):
                t = it.get("txt") or it.get("text")
                if isinstance(t, str) and t.strip():
                    out.append(t.strip())
    return " ".join(out)


def parse_body(blob):
    parts, kinds = [], set()
    try: top = pb_fields(blob)
    except ValueError: return "", "unknown"
    def walk(fields, depth):
        for f, w, v in fields:
            if w != 2: continue
            if f == 45101:
                s = utf8(v)
                if s is None:
                    try:
                        for f2, w2, v2 in pb_fields(v):
                            if w2 == 2 and f2 in (1, 2):
                                s2 = utf8(v2)
                                if s2: parts.append(s2)
                    except ValueError: pass
                elif s: parts.append(s)
                kinds.add("text")
            elif f == 47423:
                try:
                    q = []
                    for f2, w2, v2 in pb_fields(v):
                        if w2 == 2 and f2 == 45101:
                            s = utf8(v2)
                            if s: q.append(s)
                    if q: parts.append("[回复「" + " ".join(q)[:60] + "」] ")
                    kinds.add("reply")
                except ValueError: pass
            elif f in (45402, 45600): kinds.add("image")
            elif f in (45503, 45505, 45517): kinds.add("file")
            elif f == 45815:
                s = utf8(v)
                if s: parts.append(s)
                kinds.add("emoji")
            elif f == 48274:
                # 系统灰条的纯文本字段
                s = utf8(v)
                if s: parts.append(s)
                kinds.add("text")
            elif f == 48271:
                # 灰条 JSON：正文在 items[].txt
                s = utf8(v)
                if s:
                    t = _json_text(s)
                    if t: parts.append(t[:120])
                kinds.add("gray")
            elif f == 47901:
                # 卡片 JSON：prompt / desc / meta
                s = utf8(v)
                if s:
                    t = _json_text(s)
                    if t: parts.append("[卡片] " + t[:120])
                kinds.add("card")
            elif f in (48153, 48157):
                # 语音通话 / 已在其他设备处理 等系统提示
                s = utf8(v)
                if s: parts.append(s)
                kinds.add("system")
            elif f == 48178:
                # QQ 秀动态卡片
                s = utf8(v)
                if s: parts.append("[卡片] " + s[:100])
                kinds.add("card")
            elif f == 48214:
                # gtip XML 提示：去标签留文本
                s = utf8(v)
                if s:
                    t = re.sub(r"<[^>]+>", " ", s)
                    t = re.sub(r"\s+", " ", t).strip()
                    if t: parts.append(t[:100])
                kinds.add("system")
            elif depth < 3:
                try: walk(pb_fields(v), depth+1)
                except Exception: pass
    walk(top, 0)
    txt = "".join(parts).strip()
    if not txt:
        # 兜底：媒体类写 [媒体]，其余（含字段全空）写 [系统消息]，避免空正文/未知消息
        txt = "[媒体]" if kinds & {"image", "file", "emoji"} else "[系统消息]"
    if kinds == {"image", "file", "emoji"}: kind = "sticker"
    elif kinds == {"image", "file"}: kind = "pic"
    elif kinds == {"emoji", "file"}: kind = "sticker"
    else: kind = "+".join(sorted(kinds)) if kinds else "empty"
    return txt, kind

def _existing_uid_map():
    """复用已落盘的昵称映射；增量解析时旧行不需要再扫一遍。"""
    if not os.path.exists(UID_MAP):
        return {}
    try:
        with open(UID_MAP, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}
    out = {}
    for uid, info in (raw or {}).items():
        if isinstance(info, dict):
            cleaned = {"nick": info.get("nick", ""), "qq": info.get("qq", "")}
            for k in ("remark", "nick_raw"):
                if info.get(k):
                    cleaned[k] = info.get(k)
            out[str(uid)] = cleaned
    return out


def collect_messages(plain, seen=None):
    """解析消息，返回 (结构化列表, uid→昵称映射, 群号→群名)。

    seen 不为 None 时进入增量模式：先全量扫一遍 msg_id（保险网，不会漏掉
    老时间戳补写的消息），再只取未见行的正文解析。昵称映射以已落盘的
    uid_map.json 为基线，仅用新行补齐。

    昵称/QQ号直接来自消息表：c2c 40030=对方QQ 40033=发送者QQ 40093=发送者昵称；
    群聊 40090=群名片 40093=昵称。解决增量目录/发送者显示为 u_xxx 编码的问题。
    """
    con = sqlite3.connect(plain)
    uid_map = _existing_uid_map() if seen is not None else {}  # uid -> {"nick":.., "qq":..}

    def _reg(uid, qq, nick):
        if not uid or (not nick and not qq):
            return
        cur = uid_map.setdefault(uid, {"nick": "", "qq": ""})
        if nick and not cur["nick"]:
            cur["nick"] = str(nick).strip()
        if str(qq).strip() in ("0", "None", "none"):
            qq = ""
        if qq and not cur["qq"]:
            cur["qq"] = str(qq)

    def _label(uid, qq, nick, fallback=""):
        nick = (nick or "").strip()
        qq = str(qq or "").strip()
        if qq in ("0", "None", "none"):
            qq = ""          # 字段 40033 为 0 不是 QQ 号（历史坑 QQ0）
        info = uid_map.get(uid) or {}
        if not nick:
            nick = info.get("nick", "")
        if not qq:
            qq = info.get("qq", "")
        if str(qq) in ("0", "None", "none"):
            qq = ""
        if nick and qq:
            return f"{nick}({qq})"
        if nick:
            return nick
        if qq:
            return f"QQ{qq}"
        return fallback or "未知"

    def _p(pct, stage):
        if seen is not None:
            progress(pct, stage)

    def _fetch(table, sel, lo, hi):
        """返回 (库内有效行数, 待解析行)。增量模式只取未见 msg_id 的行。"""
        base = "FROM %s WHERE [40050]>? AND [40800] IS NOT NULL" % table
        if seen is None:
            rows = con.execute("SELECT %s %s" % (sel, base), (BIGBANG,)).fetchall()
            return len(rows), rows
        # 安全网：仍全量比对 msg_id，避免漏掉老时间戳补写的消息
        keys = con.execute("SELECT rowid,[40001] %s" % base, (BIGBANG,)).fetchall()
        new = [(rid, mid) for rid, mid in keys if str(mid) not in seen]
        out, step = [], 500
        for i in range(0, len(new), step):
            chunk = [rid for rid, _ in new[i:i + step]]
            q = "SELECT %s FROM %s WHERE rowid IN (%s)" % (
                sel, table, ",".join("?" * len(chunk)))
            out.extend(con.execute(q, chunk).fetchall())
            _p(lo + int((hi - lo) * min(1.0, (i + step) / max(1, len(new)))),
               "读取新消息 %d/%d" % (min(i + step, len(new)), len(new)))
        if not new:
            _p(hi, "读取新消息 0 条")
        return len(keys), out

    sel_c2c = "[40001],[40021],[40020],[40050],[40800],[40030],[40033],[40093]"
    sel_grp = "[40001],[40027],[40020],[40050],[40800],[40033],[40090],[40093]"

    _p(22, "扫描消息索引")
    n_c2c, rows = _fetch("c2c_msg_table", sel_c2c, 24, 34)
    _p(36, "扫描群消息索引")
    n_grp, grows = _fetch("group_msg_table", sel_grp, 38, 48)
    if seen is not None:
        log(f"  库内消息 {n_c2c + n_grp} 条 | 本次待解析 {len(rows) + len(grows)} 条")

    msgs = []
    # 私聊
    _p(50, "解析私聊消息")
    for _mid, peer_uid, sender_uid, _ts, _body, peer_qq, sender_qq, sender_nick in rows:
        _reg(sender_uid, sender_qq, sender_nick)
    for r in rows:
        mid, peer_uid, sender_uid, ts, body, peer_qq, sender_qq, sender_nick = r
        if not ts or not body:
            continue
        peer_label = _label(peer_uid, peer_qq, "")
        txt, kind = parse_body(body)
        dt = datetime.fromtimestamp(ts, CST)
        msgs.append({
            "key": str(mid), "ts": ts,
            "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "month": dt.strftime("%Y-%m"),
            "conv": peer_label, "conv_raw": peer_uid or "", "group": False,
            "sender": _label(sender_uid, sender_qq, sender_nick),
            "text": txt.replace("\r", " ").replace("\n", " ⏎ "),
            "type": kind,
        })
    # 群聊
    _p(54, "解析群聊消息")
    for _mid, _gnum, sender_uid, _ts, _body, sender_qq, group_card, sender_nick in grows:
        _reg(sender_uid, sender_qq, group_card or sender_nick)
    for r in grows:
        mid, gnum, sender_uid, ts, body, sender_qq, group_card, sender_nick = r
        if not ts or not body:
            continue
        gnum = str(gnum)
        txt, kind = parse_body(body)
        dt = datetime.fromtimestamp(ts, CST)
        msgs.append({
            "key": str(mid), "ts": ts,
            "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "month": dt.strftime("%Y-%m"),
            "conv": gnum, "conv_raw": gnum, "group": True,
            "sender": _label(sender_uid, sender_qq, group_card or sender_nick),
            "text": txt.replace("\r", " ").replace("\n", " ⏎ "),
            "type": kind,
        })
    # 联系人表: 41135=备注名(实测), 40094=昵称, 40093=另一昵称, 40030=对方QQ号。
    # 数字 uid 行 = 群会话(40094=群名称)。
    # 备注名优先——用户搜索/提问习惯用备注(如"胡康")而非网名。
    # remark/nick_raw 独立存储,供工作台"联系人"页分别展示备注与昵称。
    _p(58, "读取联系人")
    group_map = {}  # 群号 -> 群名称
    try:
        for uid, peer, title, alt, remark in con.execute(
                'SELECT "40021","40030","40094","40093","41135" FROM "recent_contact_v3_table"'):
            uid = str(uid or "").strip()
            if not uid:
                continue
            gname = str(title or "").strip()
            rmark = str(remark or "").strip()
            if uid.isdigit():
                # 群会话行
                if gname:
                    group_map[uid] = gname
                continue
            cur = uid_map.setdefault(uid, {"nick": "", "qq": ""})
            if rmark:
                cur["remark"] = rmark
            if gname and gname != str(alt or "").strip():
                cur["nick_raw"] = gname
            label = (rmark or gname or str(alt or "").strip())
            if not label:
                continue
            cur["nick"] = label
            if peer and not cur["qq"]:
                cur["qq"] = str(peer)
    except Exception:
        pass  # 联系人表缺失/结构变化时退回消息昵称
    con.close()
    return msgs, uid_map, group_map

def safe(s):
    return re.sub(r'[\\/:*?"<>|\r\n\t\x00-\x1f]', "_", (s or "").strip())[:60] or "unnamed"

def _conv_key(m):
    return (group_alias.normalize(m["conv"]) if m["group"]
            else user_alias.normalize(m["conv"]))


def append_incremental(new_msgs):
    """按「会话+月份」分桶写：每个文件只打开/读取一次，避免逐条扫描的平方级 IO。"""
    # 同批次必须排序：collect_messages 吐的是「新在前」，直接追加会写成日内倒序
    # （2026-10/11 的 23,967 处逆序即此因）。sorted 稳定，同秒保持原相对序。
    new_msgs = sorted(new_msgs, key=lambda m: (_conv_key(m), m["month"], m["time"]))
    buckets = {}
    for m in new_msgs:
        sub = "群聊" if m["group"] else "私聊"
        # 退役会话：用户明确删除过的不再写回（名单见 excluded.json）
        if conv_exclude.is_excluded(sub, m["conv"]):
            continue
        # 会话键归一：群聊按 group_alias（群号 vs 群内部ID），
        # 私聊按 user_alias（u_xxx / 临时会话_u_xxx / 昵称(QQ) → QQ<号>[_后缀]）。
        # 不归一就会再长出一个重复目录（2026-11-18 审计结论）。
        buckets.setdefault((sub, safe(_conv_key(m)), m["month"]), []).append(m)

    written = set()
    cnt = 0
    total = max(1, len(buckets))
    for i, ((sub, conv, month), rows) in enumerate(buckets.items(), 1):
        d = os.path.join(INCR_OUT, sub, conv)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"{month}.md")
        is_new = not os.path.exists(p)
        # 找文件里已有的最后一个日期小节,同日不重复写小节头
        last_day = None
        if not is_new:
            try:
                with open(p, encoding="utf-8") as rf:
                    for ln in rf:
                        s = ln.strip()
                        if s.startswith("## "):
                            last_day = s[3:].strip()
            except Exception:
                pass
        with open(p, "a", encoding="utf-8") as f:
            if is_new:
                f.write(f"# {conv} - {month}（增量）\n\n")
            cur_day = last_day
            for m in rows:
                day = m["time"][:10]
                if day != cur_day:
                    f.write(f"\n## {day}\n\n")
                    cur_day = day
                f.write(f"- **{m['time']}** [{m['sender'][:24]}] {m['text']}"
                        + (f" [{m['type']}]" if m["type"] not in ("text", "empty", "reply") else "") + "\n")
                cnt += 1
        written.add(p)
        progress(70 + int(18 * i / total), f"写入归档 {i}/{total} 个文件")
    return cnt, len(written)


def _write_json_both(name, data):
    """派生索引写两份：BASE（源码/配置目录）与工作台 exe 目录。"""
    targets = [os.path.join(BASE, name)]
    exe_dir = os.path.join(os.path.dirname(BASE), "ChatVault-App")
    p2 = os.path.join(exe_dir, name)
    if os.path.isdir(exe_dir) and os.path.abspath(p2) != os.path.abspath(targets[0]):
        targets.append(p2)
    for target in targets:
        try:
            tmp = target + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, target)
        except Exception:
            pass


PEERS_FILE = "c2c_peers.json"
MEMBERS_FILE = "group_members.json"


def people_index_exists():
    return (os.path.exists(os.path.join(BASE, PEERS_FILE))
            and os.path.exists(os.path.join(BASE, MEMBERS_FILE)))


def build_people_index(plain):
    """重建「私聊对象」与「群成员」两个派生索引（只读明文库）。

    - 私聊对象：c2c_msg_table 的 peer_uid / peer_qq，只有真正单聊过的人
    - 群成员：group_msg_table 按「群号+发送者」聚合，带群名片/QQ/发言数/最近发言
    不改归档、不改去重水位。
    """
    con = sqlite3.connect(plain)
    try:
        peers = con.execute(
            "SELECT DISTINCT [40021],[40030] FROM c2c_msg_table "
            "WHERE [40050]>? AND [40800] IS NOT NULL", (BIGBANG,)).fetchall()
        rows = con.execute(
            "SELECT [40027],max([40020]),max([40033]),max([40090]),max([40093]),"
            "count(*),max([40050]) FROM group_msg_table "
            "WHERE [40050]>? AND [40800] IS NOT NULL "
            "AND (([40020] IS NOT NULL AND [40020]<>'') "
            "     OR ([40033] IS NOT NULL AND [40033]<>0)) "
            "GROUP BY [40027], CASE WHEN [40020] IS NOT NULL AND [40020]<>'' "
            "THEN 'u:'||[40020] ELSE 'q:'||[40033] END",
            (BIGBANG,)).fetchall()
    finally:
        con.close()

    peer_uids, peer_qqs = set(), set()
    for uid, qq in peers:
        uid = str(uid or "").strip()
        qq = str(qq or "").strip()
        if uid:
            peer_uids.add(uid)
        if qq and qq not in ("0", "None", "none"):
            peer_qqs.add(qq)

    groups = {}
    for gnum, uid, qq, card, nick, n, last in rows:
        g = str(gnum or "").strip()
        if not g:
            continue
        qq = str(qq or "").strip()
        if qq in ("0", "None", "none"):
            qq = ""
        groups.setdefault(g, []).append({
            "uid": str(uid or "").strip(), "qq": qq,
            "nick": str(nick or "").strip(), "card": str(card or "").strip(),
            "n": int(n or 0), "last": int(last or 0),
        })
    for members in groups.values():
        members.sort(key=lambda m: (-m["n"], -m["last"]))

    stamp = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    _write_json_both(PEERS_FILE, {"generated_at": stamp,
                                  "uids": sorted(peer_uids), "qqs": sorted(peer_qqs)})
    _write_json_both(MEMBERS_FILE, {"generated_at": stamp, "groups": groups})
    return len(peer_uids), sum(len(v) for v in groups.values())


def save_uid_map(uid_map):
    """uid→昵称(QQ号) 映射落盘，供工作台显示历史 u_xxx 消息时翻译。

    同时写一份到工作台 exe 目录——定时任务跑在源码目录，若只写这里，
    打包版（读自身目录的 uid_map.json）会永远停留在过期映射。
    """
    targets = [UID_MAP]
    exe_dir = os.path.join(os.path.dirname(BASE), "ChatVault-App")
    exe_map = os.path.join(exe_dir, "uid_map.json")
    if os.path.isdir(exe_dir) and os.path.abspath(exe_map) != os.path.abspath(UID_MAP):
        targets.append(exe_map)
    for target in targets:
        try:
            tmp = target + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(uid_map, f, ensure_ascii=False)
            os.replace(tmp, target)
        except Exception:
            pass
    return len(uid_map)


def save_group_map(group_map):
    """群号→群名称 映射落盘（供工作台"联系人"页群聊列表）。

    与已有文件合并写：recent_contact_v3_table 只含"当前在群"的群，
    直接覆盖会把已退出老群的名字抹掉（2026-11-18 踩过）。
    """
    if not group_map:
        return 0
    targets = [os.path.join(BASE, "group_map.json")]
    exe_dir = os.path.join(os.path.dirname(BASE), "ChatVault-App")
    exe_map = os.path.join(exe_dir, "group_map.json")
    if os.path.isdir(exe_dir) and os.path.abspath(exe_map) != os.path.abspath(targets[0]):
        targets.append(exe_map)
    for target in targets:
        try:
            merged = {}
            if os.path.exists(target):
                try:
                    with open(target, encoding="utf-8") as f:
                        merged = json.load(f)
                except Exception:
                    merged = {}
            merged.update(group_map)
            tmp = target + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False)
            os.replace(tmp, target)
        except Exception:
            pass
    return len(group_map)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init-baseline", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    log("=" * 56); log("QQ 增量同步开始")
    if not acquire_sync_lock("ChatVaultSyncQQ"):
        log("[SKIP] 另一个 QQ 同步正在进行，本次跳过")
        progress(100, "已跳过（有同步正在运行）")
        return 0
    progress(2, "清理上次的临时文件")
    clean_stale_snapshots()
    os.makedirs(os.path.join(BASE, ".cache"), exist_ok=True)

    st = load_state()
    seen = set(st.get("seen", []))
    log(f"已去重消息: {len(seen)} 条 | 上次运行: {st.get('last_run')}")

    progress(6, "复制一份聊天数据库")
    snap, n = snapshot()
    log(f"快照完成: {n} 个文件")
    progress(10, "解密聊天数据库")
    plain = decrypt_and_merge(snap)
    if plain is None:
        log("[FATAL] 密钥失效，停止同步。请重抓 key。"); return 2

    progress(20, "读取新消息")
    # 建基线必须看全量；平时只解析未见消息
    msgs, uid_map, group_map = collect_messages(plain, None if args.init_baseline else seen)
    new = [m for m in msgs if m["key"] not in seen]

    # 人群索引：有新增或首次运行时重建（约 2.3 秒）；无新消息时沿用旧索引
    if new or not people_index_exists():
        progress(58, "整理联系人和群成员")
        n_peer, n_member = build_people_index(plain)
        log(f"人群索引: 私聊 {n_peer} 人 | 群成员 {n_member} 条")

    progress(64, "保存联系人名单")
    n_map = save_uid_map(uid_map)
    n_grp = save_group_map(group_map)
    log(f"解析完成: 待写入 {len(msgs)} 条 | 昵称映射: {n_map} 个 | 群名映射: {n_grp} 个")

    progress(66, f"筛掉重复，新增 {len(new)} 条")
    log(f"新消息: {len(new)} 条（去重后）")

    if args.init_baseline:
        allkeys = {m["key"] for m in msgs}
        st["seen"] = list(allkeys); st["total_synced"] = 0
        st["last_run"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
        save_state(st)
        log(f"[BASELINE] 已建立水位基线: {len(allkeys)} 条消息标记已见，未写入归档")
        shutil.rmtree(snap, ignore_errors=True)
        progress(100, "完成")
        return 0

    if new and not args.dry_run:
        os.makedirs(INCR_OUT, exist_ok=True)
        cnt, files = append_incremental(new)
        progress(90, "记录同步进度")
        for m in new: seen.add(m["key"])
        # 顺序无意义：避免每次对 150 万条 key 排序（原耗时 1~2 秒）
        st["seen"] = list(seen)
        st["total_synced"] = st.get("total_synced", 0) + cnt
        st["last_run"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
        progress(94, "保存同步进度")
        save_state(st)
        log(f"写入完成: {cnt} 条 → {files} 个文件 | state 已更新")
    elif new and args.dry_run:
        log("[DRY-RUN] 将写入的新消息（前10条）:")
        for m in new[:10]: log(f"  {m['time']} [{m['conv'][:16]}] {m['sender'][:12]}: {m['text'][:50]}")
    else:
        st["last_run"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
        save_state(st); log("无新消息")

    shutil.rmtree(snap, ignore_errors=True)
    progress(100, "完成")
    log("QQ 增量同步结束")
    return 0

if __name__ == "__main__":
    sys.exit(main())
