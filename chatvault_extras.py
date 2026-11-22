# -*- coding: utf-8 -*-
"""独立功能模块：收藏、统计、日志、体检、开机自启、回收站。

这些功能只读写本地配置/索引，不参与搜索/同步核心流程。
新增类似功能时优先加在这里，避免 chatvault.py 继续膨胀。
"""
import os
import re
import json
import threading
import shutil
from datetime import datetime

import local_config as cfg
import chatvault_index as vix
import conv_exclude
import archive_parse as ap


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# ---- 收藏 / 统计 / 日志 / 体检 / 回收站 / 开机自启 ----

_FAV_LOCK = threading.Lock()
FAV_FILE = os.path.join(cfg.LOCAL, "favorites.json")


def _fav_load():
    try:
        with open(FAV_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("items"), dict):
            return d
    except Exception:
        pass
    return {"items": {}}


def _fav_save(d):
    os.makedirs(cfg.LOCAL, exist_ok=True)
    tmp = FAV_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, FAV_FILE)


def _fav_key(platform, conv, date, mid):
    return "%s|%s|%s|%s" % (platform, conv, date, mid)


def api_favorites():
    with _FAV_LOCK:
        d = _fav_load()
    items = list(d["items"].values())
    items.sort(key=lambda x: str(x.get("added") or ""), reverse=True)
    return {"收藏": items, "总数": len(items)}


def api_favorite_toggle(payload):
    platform = "微信" if payload.get("platform") == "微信" else "QQ"
    conv = str(payload.get("conv") or "").strip()
    date = str(payload.get("date") or "").strip()
    try:
        mid = int(payload.get("id") or 0)
    except (TypeError, ValueError):
        mid = 0
    if not conv or not mid:
        return {"错误": "缺少消息定位信息"}
    key = _fav_key(platform, conv, date, mid)
    with _FAV_LOCK:
        d = _fav_load()
        if key in d["items"]:
            del d["items"][key]
            _fav_save(d)
            return {"on": False, "总数": len(d["items"])}
        d["items"][key] = {
            "key": key, "平台": platform, "会话": conv,
            "会话名": str(payload.get("conv_name") or conv),
            "日期": date, "id": mid, "时间": str(payload.get("time") or ""),
            "发送者": str(payload.get("sender") or ""),
            "正文": str(payload.get("text") or "")[:2000],
            "added": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _fav_save(d)
        return {"on": True, "总数": len(d["items"])}


def api_favorite_remove(key):
    with _FAV_LOCK:
        d = _fav_load()
        if key in d["items"]:
            del d["items"][key]
            _fav_save(d)
        return {"ok": True, "总数": len(d["items"])}


def api_stats_insights(platform="QQ"):
    """消息量总览与分布（基于索引库 SQL 聚合）。"""
    p = "微信" if platform == "微信" else "QQ"
    c = vix._conn()

    def one(sql, args=()):
        r = c.execute(sql, args).fetchone()
        return (r[0] if r else 0) or 0

    total = one("SELECT COUNT(*) FROM msg WHERE platform=? AND ts>0", (p,))
    convs = one("SELECT COUNT(DISTINCT conv) FROM msg WHERE platform=? AND ts>0", (p,))
    me = one("SELECT COUNT(*) FROM msg WHERE platform=? AND ts>0 AND is_self=1", (p,))
    years = [{"年": r[0], "条数": r[1]} for r in c.execute(
        "SELECT substr(CAST(ts AS TEXT),1,4) y, COUNT(*) n FROM msg "
        "WHERE platform=? AND ts>0 GROUP BY y ORDER BY y", (p,))]
    months = [{"月": r[0], "条数": r[1]} for r in c.execute(
        "SELECT substr(CAST(ts AS TEXT),1,4)||'-'||substr(CAST(ts AS TEXT),5,2) m, "
        "COUNT(*) n FROM msg WHERE platform=? AND ts>0 "
        "GROUP BY m ORDER BY m DESC LIMIT 24", (p,))][::-1]
    top_convs = [{"会话名": r[0] or r[1], "会话": r[1], "条数": r[2]} for r in c.execute(
        "SELECT conv_name, conv, COUNT(*) n FROM msg WHERE platform=? AND ts>0 "
        "GROUP BY conv ORDER BY n DESC LIMIT 10", (p,))]
    top_senders = [{"发送者": r[0] or "未知", "条数": r[1]} for r in c.execute(
        "SELECT sender, COUNT(*) n FROM msg WHERE platform=? AND ts>0 AND is_self=0 "
        "GROUP BY sender ORDER BY n DESC LIMIT 10", (p,))]
    kinds = [{"类型": r[0] or "text", "条数": r[1]} for r in c.execute(
        "SELECT kind, COUNT(*) n FROM msg WHERE platform=? AND ts>0 "
        "GROUP BY kind ORDER BY n DESC", (p,))]
    return {"平台": p, "总消息": total, "会话数": convs, "我发": me,
            "年度": years, "最近月份": months, "会话Top": top_convs,
            "发送者Top": top_senders, "类型": kinds}


def api_log_tail(which="qq", lines=300):
    path = cfg.wechat_log() if which == "wechat" else cfg.qq_log()
    if not path or not os.path.exists(path):
        return {"路径": path or "", "内容": "（日志文件不存在）"}
    try:
        size = os.path.getsize(path)
        with open(path, encoding="utf-8", errors="ignore") as f:
            f.seek(max(0, size - 65536))
            text = f.read()
        tail = text.splitlines()[-max(1, min(2000, int(lines))):]
        return {"路径": path, "内容": "\n".join(tail)}
    except Exception as e:
        return {"路径": path, "内容": "读取失败: %s" % e}


def api_health_check():
    """数据体检：索引健康度 + 归档规模（只读）。"""
    out = {"检查": [], "总览": {}, "归档": {}}
    try:
        st = vix.stats()
        c = vix._conn()

        def one(sql, args=()):
            r = c.execute(sql, args).fetchone()
            return (r[0] if r else 0) or 0

        empty = one("SELECT COUNT(*) FROM msg WHERE text IS NULL OR trim(text)=''")
        unknown = one("SELECT COUNT(*) FROM msg WHERE kind='unknown'")
        media = one("SELECT COUNT(*) FROM msg WHERE kind='media'")
        nots = one("SELECT COUNT(*) FROM msg WHERE ts IS NULL OR ts<=0")
        out["总览"] = {"消息": sum(x["消息"] for x in st["平台"].values()),
                        "会话": sum(x["会话"] for x in st["平台"].values()),
                        "索引MB": st.get("库大小MB", 0)}

        def add(name, val, ok, note=""):
            out["检查"].append({"项目": name, "结果": val,
                                "状态": "ok" if ok else "warn", "说明": note})

        add("空正文消息", empty, empty == 0, "媒体/卡片应显示占位文本")
        add("未解析类型", unknown, unknown == 0, "可尝试重建索引")
        add("媒体占位", media, True, "仅占位，不含真实图片/文件")
        add("缺失时间戳", nots, nots == 0, "会影响时间排序")
    except Exception as e:
        out["错误"] = str(e)
    try:
        for plat, root in (cfg.roots() or {}).items():
            n = size = 0
            for dirpath, _, files in os.walk(root):
                for f in files:
                    if f.endswith(".md"):
                        n += 1
                        try:
                            size += os.path.getsize(os.path.join(dirpath, f))
                        except OSError:
                            pass
            out["归档"][plat] = {"文件": n, "MB": round(size / 1048576, 1)}
    except Exception:
        pass
    return out


def _autostart_command():
    if getattr(sys, "frozen", False):
        return '"%s"' % sys.executable
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    return '"%s" "%s"' % (exe, os.path.join(cfg.BASE, "chatvault_app.py"))


def api_autostart(enable=None):
    """开机自启（HKCU Run，无需管理员）。enable=None 只查询。"""
    if os.name != "nt":
        return {"错误": "仅 Windows 支持"}
    import winreg
    name = "ChatVaultWorkbench"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as k:
            if enable is None:
                try:
                    v, _ = winreg.QueryValueEx(k, name)
                    return {"开启": True, "命令": str(v)}
                except FileNotFoundError:
                    return {"开启": False, "命令": ""}
            if enable:
                cmd = _autostart_command()
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, cmd)
                return {"开启": True, "命令": cmd}
            try:
                winreg.DeleteValue(k, name)
            except FileNotFoundError:
                pass
            return {"开启": False, "命令": ""}
    except Exception as e:
        return {"错误": str(e)}


def _trash_batches():
    root = cfg.trash_dir()
    out = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root), reverse=True):
        if not name.startswith("ChatVault删除会话-"):
            continue
        base = os.path.join(root, name)
        if not os.path.isdir(base):
            continue
        stamp = name.replace("ChatVault删除会话-", "")
        for platform in ("QQ", "微信"):
            pbase = os.path.join(base, platform)
            if not os.path.isdir(pbase):
                continue
            for sub in ("私聊", "群聊"):
                sbase = os.path.join(pbase, sub)
                if not os.path.isdir(sbase):
                    continue
                for d in sorted(os.listdir(sbase)):
                    p = os.path.join(sbase, d)
                    if not os.path.isdir(p):
                        continue
                    out.append({"批次": name, "时间": stamp, "平台": platform,
                                "类": sub, "目录": d,
                                "大小MB": round(_dir_size(p) / 1048576, 1)})
    return out


def api_trash_list():
    return {"回收站": cfg.trash_dir(), "条目": _trash_batches()}


def api_trash_restore(batch, platform, sub, dirname):
    """把回收站里的会话目录搬回归档/增量目录（不覆盖已存在目录）。"""
    if platform not in ("QQ", "微信") or sub not in ("私聊", "群聊"):
        return {"错误": "参数不合法"}
    if not batch or not dirname:
        return {"错误": "缺少批次或目录"}
    src = os.path.join(cfg.trash_dir(), batch, platform, sub, dirname)
    if not os.path.isdir(src):
        return {"错误": "回收站条目不存在"}
    incr = dirname.endswith("-增量")
    real = dirname[:-3] if incr else dirname
    roots = {src_tag: path for path, src_tag in vix.scan_roots(platform)}
    base = roots.get("incr" if incr else "archive")
    if not base:
        return {"错误": "未找到该平台的归档/增量目录"}
    dst = os.path.join(base, sub, real)
    if os.path.exists(dst):
        return {"错误": "目标目录已存在，未覆盖：%s" % dst}
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
    except Exception as e:
        return {"错误": "恢复失败: %s" % e}
    try:
        conv_exclude.remove(sub, real)
        m = re.search(r"(\d{5,})", real)
        if m:
            conv_exclude.remove(sub, m.group(1))
    except Exception:
        pass
    try:
        threading.Thread(target=lambda: vix.index_platform(platform), daemon=True).start()
    except Exception:
        pass
    return {"ok": True, "恢复至": dst}




def _clean_import_path(path):
    p = str(path or "").strip()
    if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'":
        p = p[1:-1]
    return os.path.abspath(os.path.expanduser(p)) if p else ""


def api_import_scan(path):
    """检查用户选的文件夹：有多少 md/txt/html/db、是否能被软件直接读取。"""
    p = _clean_import_path(path)
    if not p or not os.path.isdir(p):
        return {"错误": "文件夹不存在，请粘贴完整路径"}
    exts = {".md": 0, ".txt": 0, ".html": 0, ".htm": 0, ".db": 0}
    for root, _dirs, files in os.walk(p):
        for f in files:
            e = os.path.splitext(f)[1].lower()
            if e in exts:
                exts[e] += 1
    subs = [os.path.join(p, s) for s in ("私聊", "群聊")]
    has_priv = any(os.path.isdir(s) for s in subs)
    md_files = []
    for sub in subs:
        if not os.path.isdir(sub):
            continue
        for root, _dirs, files in os.walk(sub):
            for f in files:
                if f.lower().endswith(".md"):
                    md_files.append(os.path.join(root, f))
    sample = md_files[:3]
    readable = 0
    for f in sample:
        try:
            if ap.parse_file(f):
                readable += 1
        except Exception:
            pass
    stats = {"md": exts[".md"], "txt": exts[".txt"],
             "html": exts[".html"] + exts[".htm"], "db": exts[".db"]}
    can = bool(md_files and readable and has_priv)
    inner = ""
    for name in ("QQ", "微信"):
        sub = os.path.join(p, name)
        if os.path.isdir(sub) and any(os.path.isdir(os.path.join(sub, s)) for s in ("私聊", "群聊")):
            inner = name
            break
    if can:
        advice = "看起来是软件能直接读的 md 归档，可以复制到软件里，然后建索引。"
    elif inner:
        advice = "这个文件夹下面还有 QQ / 微信 子文件夹，请选到「" + inner + "」那一层再检查。"
    elif stats["db"]:
        advice = "发现数据库文件：这是原始聊天数据，请到「同步」页填数据位置并配密钥，不要复制到归档。"
    elif stats["md"] and not has_priv:
        advice = "找到 md 文件，但文件夹第一层没有「私聊 / 群聊」；请把这两个文件夹放到所选文件夹下面再导入。"
    elif stats["md"]:
        advice = "有「私聊 / 群聊」，但抽查的 md 读不出聊天内容；请确认是本软件导出的归档格式。"
    elif stats["txt"] or stats["html"]:
        advice = "找到手机导出的 txt / html。软件不直接支持这种格式；建议用手机把记录迁移到电脑端，或先整理成 md。"
    else:
        advice = "没有找到可读的聊天文件。请确认选的是手机导出后放在电脑上的文件夹。"
    return {"路径": p, "统计": stats, "归档结构": has_priv,
            "抽样可读": readable, "可导入": can, "建议": advice}


def api_import_copy(path, platform):
    """只把源文件夹「私聊/群聊」下的 md 复制到软件归档；不覆盖已有文件。"""
    platform = "微信" if platform == "微信" else "QQ"
    p = _clean_import_path(path)
    if not p or not os.path.isdir(p):
        return {"错误": "文件夹不存在"}
    target = os.path.abspath(cfg.wechat_archive() if platform == "微信" else cfg.qq_archive())
    if p == target or p.startswith(target + os.sep):
        return {"错误": "这个文件夹就是软件的聊天记录文件夹，不需要再复制"}
    copied = skipped = 0
    for sub in ("私聊", "群聊"):
        src_sub = os.path.join(p, sub)
        if not os.path.isdir(src_sub):
            continue
        for root, _dirs, files in os.walk(src_sub):
            rel = os.path.relpath(root, src_sub)
            for f in files:
                if not f.lower().endswith(".md"):
                    continue
                dst_dir = os.path.join(target, sub, rel)
                dst = os.path.join(dst_dir, f)
                if os.path.exists(dst):
                    skipped += 1
                    continue
                os.makedirs(dst_dir, exist_ok=True)
                shutil.copy2(os.path.join(root, f), dst)
                copied += 1
    if not copied and not skipped:
        return {"错误": "没有找到「私聊」或「群聊」下的 md 文件"}
    if copied:
        try:
            threading.Thread(target=lambda: vix.index_platform(platform), daemon=True).start()
        except Exception:
            pass
    return {"ok": True, "平台": platform, "复制": copied,
            "跳过": skipped, "目标": target}
