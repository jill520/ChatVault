# -*- coding: utf-8 -*-
"""
微信增量同步管道（可复制路线 · 全自动 · 不碰进程内存）

流程（每次运行）：
  1. 快照复制 PC 微信加密库（db + wal + shm）到工作区临时目录
  2. 用已提取的 all_keys.json 解密（纯 CPU，HMAC 校验；失败即告警停止）
  3. 解析所有 Msg_<hash> 会话表 → 结构化消息
  4. 用 server_id 全局去重（state.json 持久化）
  5. 新消息按「会话/月份」追加写入增量归档（只追加，绝不删改历史）

红线：
  - 不删除任何已有文件（增量目录只 append；state 只覆写自身）
  - 不读取微信进程内存（密钥一次性提取，之后长期复用）
  - 密钥失效（微信升级/换号）→ HMAC 失败 → 写 ERROR 日志并退出，等重跑 keys.hunt

用法：
  python sync_wechat.py            # 正常增量同步
  python sync_wechat.py --dry-run  # 只探测，不写入
  python sync_wechat.py --force-key-check  # 强制重新校验密钥（解密全库）
"""
import os, sys, json, shutil, hashlib, sqlite3, argparse, tempfile, time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import conv_exclude

# ---- 路径配置：优先读本地配置（.local/config.json）；回退值为历史部署的硬编码路径 ----
BASE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(BASE, "tools", "wechat4-db-decrypt")
SRC_DB = ""  # 在 .local/config.json 配置 paths.wechat_db_dir
KEYS = os.path.join(TOOL, "all_keys.json")
INCR_OUT = ""          # 在 .local/config.json 配置 paths.wechat_incr
STATE = os.path.join(BASE, "wechat_sync_state.json")
LOG = os.path.join(BASE, "wechat_sync.log")
try:
    import local_config as _lcfg
    BASE = _lcfg.APP_DIR
    if _lcfg.wechat_db_dir():
        SRC_DB = _lcfg.wechat_db_dir()
    if _lcfg.wechat_incr():
        INCR_OUT = _lcfg.wechat_incr()
    if _lcfg.wechat_log():
        STATE = os.path.join(os.path.dirname(_lcfg.wechat_log()), "wechat_sync_state.json")
        LOG = _lcfg.wechat_log()
    # 工具目录：优先 BASE，其次打包资源目录（exe 内置）
    _tool_cands = [os.path.join(BASE, "tools", "wechat4-db-decrypt"),
                   os.path.join(_lcfg.RES_DIR, "tools", "wechat4-db-decrypt")]
    TOOL = next((p for p in _tool_cands if os.path.isdir(p)), _tool_cands[0])
    KEYS = _lcfg.all_keys_file()
except Exception:
    pass
CST = timezone(timedelta(hours=8))
BIGBANG = 1700000000  # 2023-11 之前视为异常时间戳，跳过

sys.path.insert(0, os.path.join(TOOL, "src"))


def log(msg):
    line = f"[{datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


PROGRESS_TAG = "@@PROGRESS@@ "

def progress(pct, stage):
    """工作台用它画进度条；不写入日志文件。"""
    print(f"{PROGRESS_TAG}{max(0, min(100, int(pct)))}|{stage}", flush=True)


def md5(s):
    try:
        return hashlib.md5(s, usedforsecurity=False).hexdigest()
    except TypeError:
        return hashlib.md5(s).hexdigest()


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


def load_state():
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"seen": [], "last_run": None, "total_synced": 0}


def save_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE)


def snapshot(msg_dbs):
    """只复制 message_*.db 及 contact.db（含 wal/shm），到独立临时目录。"""
    tmp = tempfile.mkdtemp(prefix="wxsnap-", dir=os.path.join(BASE, ".cache"))
    os.makedirs(os.path.join(tmp, "message"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "contact"), exist_ok=True)
    copied = 0
    for rel in msg_dbs + ["contact/contact.db"]:
        src = os.path.join(SRC_DB, rel)
        if not os.path.exists(src):
            continue
        for suffix in ("", "-wal", "-shm"):
            s = src + suffix
            if os.path.exists(s):
                d = os.path.join(tmp, rel + suffix)
                os.makedirs(os.path.dirname(d), exist_ok=True)
                try:
                    shutil.copy2(s, d)
                    copied += 1
                except OSError as e:
                    log(f"  [WARN] 复制失败 {s}: {e}")
    return tmp, copied


def decrypt(snap_dir):
    """复用新工具解密核心；返回解密库目录，或 None（密钥失效）。"""
    from wxexport.decrypt import decrypt_database, merge_wal
    with open(KEYS, encoding="utf-8") as f:
        keys = json.load(f)
    # 规整 key 结构
    norm = {}
    for k, v in keys.items():
        if isinstance(v, str):
            norm[k] = {"enc_key": v.strip("x'")}
        elif isinstance(v, dict) and "enc_key" in v:
            norm[k] = v
    out = os.path.join(snap_dir, "dec")
    failed = 0
    items = [(rel, info) for rel, info in norm.items()
             if rel.startswith("message/") or rel.startswith("contact/")]
    for i, (rel, info) in enumerate(items, 1):
        progress(10 + int(30 * i / max(1, len(items))), f"解密 {rel}")
        src = os.path.join(snap_dir, rel)
        if not os.path.exists(src):
            continue
        enc_key = bytes.fromhex(info["enc_key"])
        dst = os.path.join(out, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not decrypt_database(src, dst, enc_key):
            log(f"  [ERROR] 密钥失效: {rel} HMAC 校验失败 —— 需重跑 keys.hunt")
            failed += 1
        else:
            merge_wal(src, dst, enc_key)
    if failed:
        return None
    return out


def collect_messages(dec_dir, seen=None):
    """从解密库提取会话消息（结构化），复用 msgparse。

    seen 不为 None 时只解析未见 server_id/local_id 的行；联系人、账号主人
    识别仍需全量扫描（微信库小，这一步很快）。
    """
    from wxexport.msgparse import parse_message, SYSTEM_TYPES
    contact_db = os.path.join(dec_dir, "contact", "contact.db")
    cmap = {}
    if os.path.exists(contact_db):
        con = sqlite3.connect(contact_db)
        for u, nick, remark in con.execute("SELECT username, nick_name, remark FROM contact"):
            cmap[u] = (remark or nick or u).strip() or u
        con.close()
    md5map = {md5(u.encode()): u for u in cmap}

    def _key(db, table, lid, sid):
        return str(sid) if sid else f"{os.path.basename(db)}:{table}:{lid}"

    # 识别账号主人
    from collections import Counter
    sender_counter = Counter()
    msg_dir = os.path.join(dec_dir, "message")
    dbs = []
    for f in sorted(os.listdir(msg_dir)) if os.path.isdir(msg_dir) else []:
        import re
        if re.fullmatch(r"message_\d+\.db", f):
            dbs.append(os.path.join(msg_dir, f))

    # 先收集每个会话的 sender，识别 my_wxid
    conv_meta = []
    for db in dbs:
        con = sqlite3.connect(db)
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
            for t in tables:
                name2id = {}
                try:
                    name2id = dict(con.execute("SELECT rowid, user_name FROM Name2Id"))
                except sqlite3.Error:
                    pass
                try:
                    snds = [r[0] for r in con.execute(f"SELECT DISTINCT real_sender_id FROM [{t}]")]
                except sqlite3.Error:
                    snds = []
                wxids = {name2id.get(x, "") for x in snds} - {""}
                username = md5map.get(t[4:], t[4:])
                conv_meta.append((db, t, username, name2id))
                for w in wxids:
                    sender_counter[w] += 1
        finally:
            con.close()
    my_wxid = sender_counter.most_common(1)[0][0] if sender_counter else ""

    messages = []
    n_total = 0
    total_conv = max(1, len(conv_meta))
    last_pct = -1
    for idx, (db, t, username, name2id) in enumerate(conv_meta, 1):
        pct = 45 + int(17 * idx / total_conv)
        if pct != last_pct:
            progress(pct, f"读取会话 {idx}/{total_conv}")
            last_pct = pct
        display = cmap.get(username, username)
        is_chatroom = username.endswith("@chatroom")
        con = sqlite3.connect(db)
        try:
            cols = {r[1] for r in con.execute(f"PRAGMA table_info([{t}])")}
            has_ct = "WCDB_CT_message_content" in cols
            sel = ("local_id, server_id, local_type, sort_seq, create_time, "
                   "real_sender_id, message_content" +
                   (", WCDB_CT_message_content" if has_ct else ", NULL"))
            if seen is None:
                rows = con.execute(f"SELECT {sel} FROM [{t}]").fetchall()
            else:
                keys = con.execute(
                    f"SELECT rowid, local_id, server_id FROM [{t}]").fetchall()
                n_total += len(keys)
                new_rid = [r[0] for r in keys if _key(db, t, r[1], r[2]) not in seen]
                rows = []
                for i in range(0, len(new_rid), 500):
                    chunk = new_rid[i:i + 500]
                    q = (f"SELECT {sel} FROM [{t}] WHERE rowid IN (%s)"
                         % ",".join("?" * len(chunk)))
                    rows.extend(con.execute(q, chunk).fetchall())
        except sqlite3.Error:
            rows = []
        finally:
            con.close()
        for r in rows:
            lid, sid, ltype, seq, ts, sndid, content, ct = r
            if not ts or ts < BIGBANG:
                continue
            sender_wxid = name2id.get(sndid, "")
            if my_wxid and sender_wxid == my_wxid:
                sender = "我"
            elif (ltype & 0xFFFFFFFF) in SYSTEM_TYPES:
                sender = "系统"
            elif sender_wxid and sender_wxid in cmap:
                sender = cmap[sender_wxid]
            elif sender_wxid:
                sender = sender_wxid
            else:
                sender = display
            parsed = parse_message(ltype, content, ct, is_chatroom)
            dt = datetime.fromtimestamp(ts, CST)
            messages.append({
                "key": _key(db, t, lid, sid),
                "ts": ts,
                "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
                "month": dt.strftime("%Y-%m"),
                "conv": display,
                "conv_raw": username,
                "group": is_chatroom,
                "sender": sender,
                "text": parsed["content"].replace("\r", " ").replace("\n", " ⏎ "),
                "type": parsed["type_name"],
            })
    if seen is not None:
        log(f"  库内消息 {n_total} 条 | 本次待解析 {len(messages)} 条")
    return messages, len(cmap), my_wxid


def safe(s):
    import re
    return re.sub(r'[\\/:*?"<>|\r\n\t\x00-\x1f]', "_", (s or "").strip())[:60] or "unnamed"


def append_incremental(new_msgs):
    """按「会话+月份」分桶写：每个文件只打开一次，返回写入条数与文件数。"""
    buckets = {}
    for m in new_msgs:
        sub = "群聊" if m["group"] else "私聊"
        # 退役会话：用户明确删除过的不再写回（excluded.json）
        if (conv_exclude.is_excluded(sub, m["conv"])
                or conv_exclude.is_excluded(sub, m.get("conv_raw", ""))):
            continue
        dirname = f"{safe(m['conv'])}({safe(m['conv_raw'])})"
        buckets.setdefault((sub, dirname, m["month"], m["conv"]), []).append(m)

    written_files = set()
    written = 0
    total = max(1, len(buckets))
    for i, ((sub, dirname, month, conv), rows) in enumerate(buckets.items(), 1):
        d = os.path.join(INCR_OUT, sub, dirname)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"{month}.md")
        is_new_file = not os.path.exists(p)
        with open(p, "a", encoding="utf-8") as f:
            if is_new_file:
                f.write(f"# {conv} - {month}（增量）\n\n")
            for m in rows:
                f.write(f"- **{m['time'][11:]}** [{m['sender']}] {m['text']}"
                        + (f" [{m['type']}]" if m["type"] and m["type"] != "文本" else "") + "\n")
                written += 1
        written_files.add(p)
        progress(70 + int(18 * i / total), f"写入归档 {i}/{total} 个文件")
    return written, len(written_files)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-snapshot", action="store_true", help="直接解密 config 里的 decrypted 目录（调试）")
    ap.add_argument("--init-baseline", action="store_true", help="首次运行：把当前全部消息标记为已见，不写归档（建立水位基线）")
    args = ap.parse_args()
    if not acquire_sync_lock("ChatVaultSyncWeChat"):
        log("[SKIP] 另一个微信同步正在进行，本次跳过")
        progress(100, "已跳过（有同步正在运行）")
        return 0

    os.makedirs(os.path.join(BASE, ".cache"), exist_ok=True)
    # 微信解密工具的可写配置：exe 内 _MEIPASS 退出即丢，必须落到数据目录
    try:
        _wc = _lcfg.ensure_wx_tool_config()
        os.environ["WXEXPORT_CONFIG"] = _wc["配置"]
        os.environ["WXEXPORT_KEYS_FILE"] = _wc["密钥"]
        os.environ["WXEXPORT_DB_DIR"] = _lcfg.wechat_db_dir() or ""
        os.environ["WXEXPORT_HUNTED_FILE"] = os.path.join(_wc["目录"], "hunted_keys.txt")
    except Exception:
        pass
    log("=" * 56)
    log("微信增量同步开始")

    st = load_state()
    seen = set(st.get("seen", []))
    log(f"已去重消息: {len(seen)} 条 | 上次运行: {st.get('last_run')}")

    progress(4, "复制一份微信数据库")
    if args.no_snapshot:
        dec_dir = os.path.join(TOOL, "decrypted_v2")
        if not os.path.isdir(dec_dir):
            log("[ERROR] 调试目录不存在"); return 1
        tmp = None
    else:
        snap_dir, n = snapshot(
            ["message/message_0.db", "message/message_1.db",
             "message/message_2.db", "message/message_3.db"])
        log(f"快照完成: {n} 个文件")
        progress(8, "解密聊天数据库")
        dec_dir = decrypt(snap_dir)
        tmp = snap_dir
        if dec_dir is None:
            log("[FATAL] 密钥失效，停止同步。请重跑 keys.hunt 提取新密钥。")
            return 2

    progress(42, "读取会话列表")
    # 建基线时必须看全量；平时只解析未见消息
    msgs, ncontact, my = collect_messages(dec_dir, None if args.init_baseline else seen)
    log(f"解析完成: 待写入 {len(msgs)} 条 | 联系人 {ncontact} | 我={my}")

    new = [m for m in msgs if m["key"] not in seen]
    progress(66, f"筛掉重复，新增 {len(new)} 条")
    log(f"新消息: {len(new)} 条（去重后）")

    if args.init_baseline:
        # 建立基线：当前库全部消息标记已见，不写归档（避免与平板历史归档重复）
        allkeys = {m["key"] for m in msgs}
        st["seen"] = list(allkeys)
        st["total_synced"] = 0
        st["last_run"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
        save_state(st)
        log(f"[BASELINE] 已建立水位基线: {len(allkeys)} 条消息标记已见，未写入归档")
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        progress(100, "完成")
        return 0

    if new and not args.dry_run:
        os.makedirs(INCR_OUT, exist_ok=True)
        progress(70, "写入聊天记录")
        cnt, files = append_incremental(new)
        progress(90, "记录同步进度")
        for m in new:
            seen.add(m["key"])
        st["seen"] = list(seen)
        st["total_synced"] = st.get("total_synced", 0) + cnt
        st["last_run"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
        progress(94, "保存同步进度")
        save_state(st)
        log(f"写入完成: {cnt} 条 → {files} 个文件 | state 已更新")
    elif new and args.dry_run:
        log("[DRY-RUN] 以下为将被写入的新消息（前10条）:")
        for m in new[:10]:
            log(f"  {m['time']} [{m['conv']}] {m['sender']}: {m['text'][:50]}")
    else:
        st["last_run"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
        save_state(st)
        log("无新消息")

    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)  # 仅清理自建临时快照目录
    progress(100, "完成")
    log("微信增量同步结束")
    return 0


if __name__ == "__main__":
    sys.exit(main())
