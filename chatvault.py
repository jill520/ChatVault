#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chatvault.py - 个人聊天数据本地检索与对话工作台

安全红线（不可妥协）:
  - 只监听 127.0.0.1,绝不对外网暴露
  - 搜索/浏览全程本地,不发任何网络请求
  - 对话功能: 任何送给 GLM 的文本都强制经过本地 sanitize.py 脱敏
  - 只读归档,不删除、不覆盖任何原始文件
  - 只用 Python 标准库,零第三方依赖

功能:
  1. 搜索: 遍历归档 Markdown,关键词/正则检索,显示上下文
  2. 对话: 自由对话 + 可选归档片段作上下文,强制脱敏后送 GLM
  3. 密钥: 显示 QQ/GLM 密钥状态、活跃库路径、最后同步时间;
           "测试密钥"复用 sync_qq.py --dry-run 验证,失效时提示重新提取

启动:
  python chatvault.py            # 默认 http://127.0.0.1:8765
  python chatvault.py --port 9000
"""
import os
import sys
import re
import io
import json
import glob
import html
import shutil
import time
import argparse
import subprocess
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote
from datetime import datetime, timedelta

import sanitize
import local_config as cfg
import archive_parse as ap
import chatvault_index as vix
import conv_exclude
import chatvault_extras as extras

BASE = os.path.dirname(os.path.abspath(__file__))
# 全部路径/密钥来自本地配置,源码内不含任何真实值
ROOTS = cfg.roots()
QQ_KEYFILE = cfg.qq_key_file()
QQ_LIB = cfg.qq_lib()
QQ_LOG = cfg.qq_log()
WX_LOG = cfg.wechat_log()

VERSION = "1.3.0"
# 删除会话的回收站：优先读设置页 paths.trash，未配置时由 local_config.trash_dir() 回退
# （旧默认 F:\垃圾中转站 → 用户主目录\ChatVault回收站），不再硬编码。

# 模型服务: 多 Provider(云端 API / 本地 Ollama、LM Studio),全部 OpenAI 兼容协议

def refresh_runtime():
    """设置页保存配置后调用：刷新运行时常量,使新配置立即生效。"""
    global ROOTS, QQ_KEYFILE, QQ_LIB, QQ_LOG, WX_LOG
    ROOTS = cfg.roots()
    QQ_KEYFILE = cfg.qq_key_file()
    QQ_LIB = cfg.qq_lib()
    QQ_LOG = cfg.qq_log()
    WX_LOG = cfg.wechat_log()


def load_glm_key():
    """从本地配置读取 GLM key,源码不硬编码(兼容旧字段)。"""
    return cfg.glm_key()


# ---------------------------------------------------------------- 首次向导 / 环境自检

def _webview2_version():
    """检测系统 WebView2 运行时版本（有则返回版本号，否则空串）。"""
    pats = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\EdgeWebView\Application\*\msedgewebview2.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\EdgeWebView\Application\*\msedgewebview2.exe"),
    ]
    best = ""
    for pat in pats:
        for p in glob.glob(pat):
            v = os.path.basename(os.path.dirname(p))
            if v and (not best or v > best):
                best = v
    return best


def _scan_named(base, names, want_dir=False, max_depth=6):
    out = []
    if not base or not os.path.isdir(base):
        return out
    base = os.path.abspath(base)
    for root, dirs, files in os.walk(base):
        if root.count(os.sep) - base.count(os.sep) >= max_depth:
            dirs[:] = []
        for n in (dirs if want_dir else files):
            if n.lower() in names:
                out.append(os.path.join(root, n))
    return out


def _local_qq_dbs():
    home = os.path.expanduser("~")
    seen, out = set(), []
    for p in _scan_named(os.path.join(home, "Documents", "Tencent Files"),
                         {"nt_msg.db"}, max_depth=6):
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out[:5]


def _local_wechat_dirs():
    home = os.path.expanduser("~")
    seen, out = set(), []
    for base in (os.path.join(home, "Documents", "WeChat Files"),
                 os.path.join(home, "Documents", "xwechat_files")):
        for p in _scan_named(base, {"db_storage"}, want_dir=True, max_depth=5):
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out[:5]


def api_preflight():
    """环境自检：WebView2、数据根可写、磁盘、已有归档、本机 QQ/微信库探测。"""
    checks = []
    wv = _webview2_version()
    checks.append({"项": "系统网页组件",
                   "值": wv or "未检测到（Win10 可能需安装 Evergreen Runtime）",
                   "正常": bool(wv)})
    root = cfg.data_root()
    writable, err = False, ""
    try:
        os.makedirs(root, exist_ok=True)
        t = os.path.join(root, ".cv_write_test")
        with open(t, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(t)
        writable = True
    except Exception as e:
        err = str(e)
    checks.append({"项": "主文件夹可写",
                   "值": root + ("" if writable else "（%s）" % err),
                   "正常": writable})
    try:
        free = shutil.disk_usage(root).free / 1073741824.0
        checks.append({"项": "磁盘剩余", "值": "%.1f GB" % free, "正常": free >= 2})
    except Exception as e:
        checks.append({"项": "磁盘剩余", "值": str(e), "正常": False})
    return {
        "检查": checks,
        "数据根": root,
        "数据根来源": "已配置" if (cfg.get_all().get("paths", {}) or {}).get("data_root") else "默认",
        "路径": {"QQ归档": cfg.qq_archive(), "微信归档": cfg.wechat_archive(),
                 "QQ增量": cfg.qq_incr(), "微信增量": cfg.wechat_incr(),
                 "索引": cfg.index_db(), "回收站": cfg.trash_dir()},
        "已有归档": {"QQ": cfg._has_md(cfg.qq_archive()),
                     "微信": cfg._has_md(cfg.wechat_archive())},
        "路径存在": {
            "QQ归档": os.path.isdir(cfg.qq_archive()),
            "微信归档": os.path.isdir(cfg.wechat_archive()),
            "QQ增量": os.path.isdir(cfg.qq_incr()),
            "微信增量": os.path.isdir(cfg.wechat_incr()),
            "索引": os.path.isdir(os.path.dirname(cfg.index_db()) or "."),
            "回收站": os.path.isdir(cfg.trash_dir()),
        },
        "原始": {"QQ": cfg.qq_lib(), "微信": cfg.wechat_db_dir()},
        "原始状态": {
            "QQ": bool(cfg.qq_lib() and os.path.exists(cfg.qq_lib())),
            "微信": bool(cfg.wechat_db_dir() and os.path.isdir(cfg.wechat_db_dir())),
        },
        "探测": {"QQ库": _local_qq_dbs(), "微信库": _local_wechat_dirs()},
    }


SKILL_ALLOW = {"qq-key-recovery"}


def api_skill_text(name):
    """返回完整 skill 正文，供界面一键复制给 AI 会话。只允许白名单。"""
    name = str(name or "").strip()
    if name not in SKILL_ALLOW:
        return {"错误": "未知 skill"}
    for base in (cfg.RES_DIR, BASE):
        p = os.path.join(base, "assets", "skills", name, "SKILL.md")
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                return {"名称": name, "内容": f.read()}
        except OSError as e:
            return {"错误": "读取 skill 失败: %s" % e}
    return {"错误": "skill 文件缺失"}


def api_setup_state():
    """每次启动都校验：路径被删/换盘后重新引导，而不是凭 setup.json 放行。"""
    ready = cfg.paths_ready()
    d = api_preflight()
    d["已完成"] = ready
    d["配置路径"] = cfg.config_path()
    d["密钥"] = key_flags()
    return d


def api_setup_paths(payload):
    """向导第 1 步：保存数据根与可选路径，创建缺失目录并返回校验结果。"""
    p = cfg.get_all()
    paths = p.setdefault("paths", {})
    for k in ("data_root", "qq_archive", "wechat_archive", "index_db", "trash",
              "qq_lib", "wechat_db_dir"):
        if k in payload:
            paths[k] = str(payload.get(k) or "").strip()
    try:
        cfg.save_config(p)
    except Exception as e:
        return {"错误": "保存配置失败: %s" % e}
    refresh_runtime()
    conv_exclude.reset()
    vix.reset_conn()
    created = []
    try:
        made = cfg.ensure_layout()
        created.extend(made.get("新建", []))
    except Exception as e:
        return {"错误": "创建数据目录失败: %s" % e}
    for fn in (cfg.qq_archive, cfg.wechat_archive, cfg.qq_incr, cfg.wechat_incr):
        try:
            d = fn()
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
                created.append(d)
        except Exception:
            pass
    ready = cfg.paths_ready()
    pre = api_preflight()
    return {"ok": True, "已完成": ready, "数据根": cfg.data_root(),
            "路径": {"QQ归档": cfg.qq_archive(), "微信归档": cfg.wechat_archive(),
                     "索引": cfg.index_db(), "回收站": cfg.trash_dir()},
            "新建": created, "检查": pre.get("检查", []),
            "路径存在": pre.get("路径存在", {}),
            "原始": pre.get("原始", {}),
            "原始状态": pre.get("原始状态", {}),
            "密钥": key_flags()}


def api_setup_init(data_root):
    """向导第 1 步：保存数据根并创建标准子目录。"""
    root = (data_root or "").strip()
    if not root:
        return {"错误": "请先选择数据根目录"}
    payload = cfg.get_all()
    payload.setdefault("paths", {})["data_root"] = os.path.abspath(root)
    cfg.save_config(payload)
    refresh_runtime()
    conv_exclude.reset()
    vix.reset_conn()
    made = cfg.ensure_layout()
    return {"ok": True, "数据根": cfg.data_root(), "新建": made.get("新建", []),
            "路径": {"QQ归档": cfg.qq_archive(), "微信归档": cfg.wechat_archive(),
                     "QQ增量": cfg.qq_incr(), "微信增量": cfg.wechat_incr(),
                     "索引": cfg.index_db(), "回收站": cfg.trash_dir()}}


def api_setup_done():
    cfg.mark_setup_done()
    return {"ok": True}


# ---------------------------------------------------------------- 设置

def api_get_config():
    """返回当前配置给设置页（本地回环,含真实值）。"""
    data = cfg.get_all()
    data["_配置路径"] = cfg.config_path()
    data["_已配置"] = cfg.is_configured()
    data["_数据根"] = cfg.data_root()
    data["_回收站"] = cfg.trash_dir()
    data["_索引库"] = cfg.index_db()
    return data


def api_save_config(payload):
    """保存设置页提交的配置,热重载并刷新运行时。"""
    try:
        cfg.save_config(payload)
        refresh_runtime()
        if (payload.get("paths") or {}).get("data_root"):
            try:
                cfg.ensure_layout()
            except Exception:
                pass
        # 索引库/回收站路径可能变了：重算排除名单位置、断开旧索引连接
        conv_exclude.reset()
        vix.reset_conn()
        return {"成功": True, "已配置": cfg.is_configured()}
    except Exception as e:
        return {"成功": False, "错误": str(e)}


def api_test_config():
    """校验当前配置：归档目录是否存在、模型服务是否可用。"""
    checks = []
    for label, path in [("QQ归档", cfg.qq_archive()), ("微信归档", cfg.wechat_archive())]:
        checks.append({"项": label, "值": path or "(未填)",
                       "正常": bool(path and os.path.isdir(path))})
    p = cfg.provider_at(cfg.active_provider())
    local = cfg.is_local_api(p.get("api"))
    ok = bool(p.get("api") and p.get("model") and (local or p.get("key")))
    checks.append({"项": "当前模型服务",
                   "值": "%s(%s%s)" % (p.get("name") or "(未命名)",
                                       p.get("model") or "未填模型名",
                                       "·本地" if local else ""),
                   "正常": ok})
    checks.append({"项": "QQ密钥文件", "值": cfg.qq_key_file() or "(未填)",
                   "正常": bool(cfg.qq_key_file() and os.path.exists(cfg.qq_key_file()))})
    trash = cfg.trash_dir()
    checks.append({"项": "删除回收站", "值": trash or "(未填)",
                   "正常": bool(trash and os.path.isdir(os.path.dirname(trash)))})
    idx = cfg.index_db()
    checks.append({"项": "检索索引库", "值": idx or "(未填)",
                   "正常": bool(idx and os.path.isdir(os.path.dirname(idx)))})
    return {"检查": checks}


def mask_key(key):
    if not key:
        return "(未配置)"
    if len(key) <= 10:
        return key[:3] + "***"
    return key[:8] + "..." + key[-4:]


# ---------------------------------------------------------------- 搜索

def list_conversations(platform):
    root = os.path.join(ROOTS[platform], "私聊")
    out = []
    if os.path.isdir(root):
        for d in sorted(os.listdir(root)):
            p = os.path.join(root, d)
            if os.path.isdir(p):
                out.append(d)
    return out


def scan_roots(platform):
    """检索范围: 归档根 + 增量同步目录(今天的新消息在增量里,未合并进归档根)。"""
    roots = []
    r = ROOTS.get(platform)
    if r and os.path.isdir(r):
        roots.append(r)
    incr = cfg.qq_incr() if platform == "QQ" else cfg.wechat_incr()
    if incr and os.path.isdir(incr) and incr != r:
        roots.append(incr)
    return roots


def search_archive(platform, query, use_regex, conv_filter, limit):
    """兼容旧签名的检索入口：现在走索引，返回结构化结果（不再是 Markdown 原始行）。"""
    r = vix.search(platform, query, use_regex=use_regex, conv=conv_filter, limit=limit)
    if r.get("错误"):
        return [{"error": r["错误"]}]
    return r.get("结果", [])


def api_search(platform, q, use_regex, conv, date_from, date_to, sender,
               include_all, offset, limit):
    """检索接口：总数 + 分页 + 会话聚合 + 用时。"""
    if vix.is_empty():
        return {"错误": "索引为空，请先到「密钥与索引」页点一次『全量重建』", "结果": [], "总数": 0}
    if not (q or "").strip() and not (conv or sender or date_from or date_to):
        return {"结果": [], "总数": 0, "提示": "输入关键词，或先选一个会话/发送者"}
    return vix.search(platform, q, use_regex=use_regex, conv=conv,
                      date_from=date_from, date_to=date_to, sender=sender,
                      include_all=include_all, offset=offset, limit=limit)


def api_conv(platform, conv, date, anchor_id, nav=0):
    """阅读器接口：取某会话某天的全部消息 + 有消息的日期列表（用于前后翻）。"""
    days = vix.conv_days(platform, conv)
    if nav and days:
        cur = date or ""
        idx = next((i for i, d in enumerate(days) if d["日期"] == cur), -1)
        if idx < 0:
            idx = len(days) - 1 if nav > 0 else 0
        else:
            idx = max(0, min(len(days) - 1, idx + nav))
        date = days[idx]["日期"]
        anchor_id = 0
    msgs, mode, used = vix.window(platform, conv, date=date, anchor_id=anchor_id)
    name = msgs[0]["会话名"] if msgs else ""
    if not name:
        for m in vix.convs(platform):
            if m["会话"] == conv:
                name = m["会话名"]
                break
    return {"消息": msgs, "日期列表": days, "日期": used, "模式": mode,
            "会话": conv, "会话名": name, "总数": vix.count(platform, conv)}


def api_convs(platform):
    return {"会话": vix.convs(platform)}


def api_calendar(platform, year=0, month=0):
    """时间表：某个月每天的条数；不传年月时自动落在最近有记录的月份。"""
    if vix.is_empty():
        return {"错误": "索引为空，请先到「密钥与索引」页点一次『全量重建』"}
    try:
        year, month = int(year or 0), int(month or 0)
    except (TypeError, ValueError):
        return {"错误": "年月不对，应该是数字"}
    rng = vix.ts_range(platform)
    if not (year and month):
        mx = str(rng.get("最晚") or "")
        if len(mx) >= 6 and mx.isdigit():
            year, month = int(mx[:4]), int(mx[4:6])
        else:
            now = datetime.now()
            year, month = now.year, now.month
    if not (2000 <= year <= 2100 and 1 <= month <= 12):
        return {"错误": "年月超出范围，只支持 2000~2100 年"}
    d = vix.calendar_month(platform, year, month)
    total = sum(d["天数"].values()) + d["未知"]
    return {"年": d["年"], "月": d["月"], "天数": d["天数"], "未知": d["未知"],
            "总计": total, "最早": rng.get("最早", 0), "最晚": rng.get("最晚", 0)}


def _probe_conv_dirs(platform, conv):
    """按会话身份键找出归档/增量里对应的实际目录（只按键匹配，不接受前端传路径）。"""
    R = vix.resolver()
    out = []
    for root, src in vix.scan_roots(platform):
        for sub in ("私聊", "群聊"):
            base = os.path.join(root, sub)
            if not os.path.isdir(base):
                continue
            for d in sorted(os.listdir(base)):
                p = os.path.join(base, d)
                if not os.path.isdir(p):
                    continue
                try:
                    _kind, key, name, qq = R.conv(os.path.join(p, "_probe_.md"), root)
                except Exception:
                    continue
                if key == conv:
                    out.append({"来源": src, "根": root, "类": sub, "目录": d,
                                "路径": p, "名称": name, "号": qq})
    return out


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def api_conv_delete(platform, conv, protect=True, dry=False):
    """整场删除：归档+增量目录移到中转站、清索引、可选登记排除名单。

    不真删任何文件；目标目录已存在时自动加后缀，绝不覆盖。
    """
    platform = "微信" if platform == "微信" else "QQ"
    conv = (conv or "").strip()
    if not conv:
        return {"错误": "缺少会话参数"}
    hits = _probe_conv_dirs(platform, conv)
    if not hits:
        return {"错误": "未找到该会话的归档目录（可能已被删除）"}
    name = next((h["名称"] for h in hits if h["名称"]), conv)
    n = vix.count(platform, conv)
    size = sum(_dir_size(h["路径"]) for h in hits)
    plan = [{"来源": h["来源"], "类": h["类"], "目录": h["目录"]} for h in hits]
    if dry:
        return {"ok": True, "预览": True, "会话": conv, "会话名": name,
                "条数": n, "占用MB": round(size / 1048576, 1), "目录": plan,
                "中转站": cfg.trash_dir()}

    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = os.path.join(cfg.trash_dir(), "ChatVault删除会话-" + stamp)
    moved = []
    try:
        for h in hits:
            dst_dir = os.path.join(base, platform, h["类"])
            os.makedirs(dst_dir, exist_ok=True)
            leaf = h["目录"] + ("-增量" if h["来源"] == "incr" else "")
            dst = os.path.join(dst_dir, leaf)
            k = 2
            while os.path.exists(dst):
                dst = os.path.join(dst_dir, leaf + "-%d" % k)
                k += 1
            shutil.move(h["路径"], dst)
            moved.append((h["路径"], dst, h["类"], h["目录"]))
    except Exception as e:
        for src, dst, _sub, _d in reversed(moved):
            try:
                os.makedirs(os.path.dirname(src), exist_ok=True)
                shutil.move(dst, src)
            except Exception:
                pass
        return {"错误": "移动失败，已回滚: %s" % e}

    idx = vix.purge_conv(platform, conv)
    excl = []
    if protect:
        for _src, _dst, sub, d in moved:
            if ("%s/%s" % (sub, d)) not in excl:
                excl.append("%s/%s" % (sub, d))
            conv_exclude.add(sub, d)
            qqno = next((h["号"] for h in hits if h["目录"] == d), "")
            if qqno:
                conv_exclude.add(sub, qqno)
    return {"ok": True, "会话": conv, "会话名": name, "条数": n,
            "释放MB": round(size / 1048576, 1), "目录": plan,
            "中转站": base, "索引": idx, "已登记排除": excl,
            "提示": "INDEX.md 未重建；如需人看的汇总表，运行 rebuild_index.py"}


def _conv_name(platform, conv):
    for m in vix.convs(platform):
        if m["会话"] == conv:
            return m["会话名"] or conv
    return conv


def export_bytes(platform, conv, fmt="md", date_from="", date_to=""):
    """把一场对话渲染成 md/txt/html 字节流（单场导出用）。"""
    name = _conv_name(platform, conv)
    buf = io.BytesIO()
    if fmt == "html":
        esc = html.escape
        rows = list(vix.iter_conv(platform, conv, date_from, date_to))
        css = ("body{margin:0;background:#f2f3f7;color:#26292e;"
               "font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif}"
               ".wrap{max-width:860px;margin:0 auto;padding:28px 18px 60px}"
               "h1{font-size:22px;margin:0 0 6px}"
               ".meta{color:#8a9099;font-size:13px;margin-bottom:22px}"
               "h2{font-size:13px;color:#8a9099;font-weight:500;text-align:center;margin:26px 0 12px}"
               ".m{display:flex;flex-direction:column;margin:0 0 12px;align-items:flex-start}"
               ".m.me{align-items:flex-end}.w{font-size:11.5px;color:#8a9099;margin:0 4px 3px}"
               ".b{max-width:76%;background:#fff;border:1px solid #e6e8ed;border-radius:14px;"
               "padding:9px 13px;font-size:13.5px;line-height:1.65;white-space:pre-wrap;"
               "word-break:break-word}.m.me .b{background:#0099ff;border-color:#0099ff;color:#fff}")
        parts = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
                 '<meta name="viewport" content="width=device-width,initial-scale=1">',
                 '<title>' + esc(name) + '</title><style>' + css + '</style></head><body>',
                 '<div class="wrap"><h1>' + esc(name) + '</h1><div class="meta">'
                 + esc(platform) + ' · ' + str(len(rows)) + ' 条</div>']
        cur = None
        for r in rows:
            if r["日期"] != cur:
                cur = r["日期"]
                parts.append('<h2>' + esc(cur or "") + '</h2>')
            who = "我" if r["我"] else (r["发送者"] or "未知")
            cls = " me" if r["我"] else ""
            parts.append('<div class="m' + cls + '"><div class="w">'
                         + esc(r["时间"] or "") + ' ' + esc(who) + '</div><div class="b">'
                         + esc(r["正文"] or "") + '</div></div>')
        parts.append("</div></body></html>")
        return name, "".join(parts).encode("utf-8")
    if fmt == "txt":
        for r in vix.iter_conv(platform, conv, date_from, date_to):
            who = "我" if r["我"] else (r["发送者"] or "未知")
            buf.write(("%s %s %s: %s\n" % (r["日期"], r["时间"], who,
                                           r["正文"] or "")).encode("utf-8"))
        return name, buf.getvalue()
    buf.write(("# %s\n\n" % name).encode("utf-8"))
    cur = None
    for r in vix.iter_conv(platform, conv, date_from, date_to):
        if r["日期"] != cur:
            cur = r["日期"]
            buf.write(("\n## %s\n\n" % cur).encode("utf-8"))
        who = "我" if r["我"] else (r["发送者"] or "未知")
        lines = (r["正文"] or "").splitlines() or [""]
        buf.write(("- **%s %s**: %s\n" % (r["时间"], who, lines[0])).encode("utf-8"))
        for extra in lines[1:]:
            buf.write(("  %s\n" % extra).encode("utf-8"))
    return name, buf.getvalue()


def api_senders(platform, conv=""):
    """发送者清单（筛选下拉用）。"""
    return {"发送者": vix.senders(platform, conv)}


def _build_index_job(force, log_fn):
    for plat in ("QQ", "微信"):
        log_fn("开始处理 %s …" % plat)
        r = vix.index_platform(plat, log=log_fn, force=force)
        log_fn("%s 完成：%d 条" % (plat, r["消息"]))
    return 0


def api_index_build(force):
    return start_job("重建检索索引" if force else "更新检索索引",
                     lambda log_fn: _build_index_job(force, log_fn))


def api_index_status():
    st = vix.stats()
    st["进度"] = vix.build_progress()
    st["版本"] = VERSION
    return st


def ensure_index_async():
    """启动时如果索引为空/落后，后台静默补一遍，不阻塞界面。"""
    def run():
        # 先让首屏/会话列表加载完，再跑索引，避免一开始就抢磁盘
        time.sleep(8)
        try:
            if vix.is_empty():
                for plat in ("QQ", "微信"):
                    vix.index_platform(plat)
            else:
                for plat in ("QQ", "微信"):
                    vix.index_platform(plat)  # 增量：只处理变化的文件
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


def gather_context(platform, query, max_chars=12000):
    """为对话收集相关归档片段(用于可选上下文)。"""
    hits = search_archive(platform, query, False, "", 40)
    parts = []
    total = 0
    for h in hits:
        if "error" in h:
            continue
        piece = "[%s %s]\n%s\n" % (h["来源"], h["行号"], h["命中"])
        if total + len(piece) > max_chars:
            break
        parts.append(piece)
        total += len(piece)
    return "\n".join(parts)


def _parse_ym(path):
    m = re.search(r"(\d{4})-(\d{2})\.md$", path)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _tail_of(f, max_lines):
    try:
        with open(f, encoding="utf-8", errors="ignore") as fh:
            lines = [l.rstrip() for l in fh.read().splitlines() if l.strip()]
    except Exception:
        return ""
    return "\n".join(lines[-max_lines:])


def recent_lines(platform, max_chars=8000, max_lines=40):
    """当月(按文件名年月)会话文件的末尾消息。mtime 在全量重写后不可靠,不使用。"""
    roots = scan_roots(platform)
    if not roots:
        return ""
    cur = "%d-%02d" % (datetime.now().year, datetime.now().month)
    cur_files = []
    all_month = []
    for r in roots:
        incr = r not in ROOTS.values()
        for f in glob.glob(os.path.join(r, "**", "*.md"), recursive=True):
            if os.path.basename(f).upper() == "INDEX.MD":
                continue
            ym = _parse_ym(f)
            if not ym:
                continue
            all_month.append((ym, f, incr))
            if "%d-%02d" % ym == cur:
                cur_files.append((f, incr))
    picks = cur_files[:4]
    if not picks and all_month:
        all_month.sort(key=lambda x: x[0], reverse=True)
        seen = set()
        for ym, f, incr in all_month:
            key = (ym, incr)
            if key in seen:
                continue
            seen.add(key)
            picks.append((f, incr))
            if len(picks) >= 3:
                break
    parts = []
    total = 0
    for f, incr in picks:
        rel = None
        for r in roots:
            try:
                cand = os.path.relpath(f, r)
                if not cand.startswith(".."):
                    rel = cand
                    break
            except ValueError:
                continue
        label = ("(增量) " if incr else "") + (rel or os.path.basename(f))
        tail = _tail_of(f, max_lines)
        if not tail:
            continue
        piece = "归档 %s 末尾最近消息:\n%s\n" % (label, tail)
        if total + len(piece) > max_chars:
            break
        parts.append(piece)
        total += len(piece)
    return "\n".join(parts)


def _today_scan(platform):
    """扫当月文件，返回今天的消息（已过统一解析层 + 发送者翻译）。

    返回 [(kind, conv_name, [msg...], 是否近期回退)]。
    微信归档没有 ## 日期 小节（date 落在 YYYY-MM-00），无法确定"哪天"，
    只能按整月文件兜底，并标注为"近期"。
    """
    roots = scan_roots(platform)
    if not roots:
        return []
    R = vix.resolver()
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    cur = now.strftime("%Y-%m")
    out = []
    for root in roots:
        for f in glob.glob(os.path.join(root, "**", cur + ".md"), recursive=True):
            if os.path.basename(f).upper() == "INDEX.MD":
                continue
            try:
                kind_c, conv, cname, _qq = R.conv(f, root)
            except Exception:
                continue
            msgs = ap.parse_file(f)
            if not msgs:
                continue
            days = {m["date"] for m in msgs}
            dated = any(d and not d.endswith("-00") for d in days)
            if dated:
                picked = [m for m in msgs if m["date"] == today]
                recent = False
            else:
                picked = msgs[-30:]
                recent = True
            if picked:
                out.append((kind_c, cname, picked, recent))
    return out


def _fmt_msg(m, R, with_sender=True):
    nm, _qq, mine = R.sender(m["sender_raw"])
    k = ap.classify(m["text"])
    body = ap.pretty_text(m["text"], k)
    if m.get("reply"):
        body = "（回复「%s」）%s" % (m["reply"], body)
    who = "我" if mine else nm
    if not with_sender:
        return body
    return "%s %s: %s" % (m["time"][:5], who, body)


def today_lines(platform, max_chars=8000):
    """今天各会话的消息原文（已整理成"时间 发送者: 内容"，不再是 Markdown 原始行）。"""
    rows = _today_scan(platform)
    if not rows:
        return ""
    R = vix.resolver()
    today = datetime.now().strftime("%Y-%m-%d")
    parts, total = [], 0
    for kind_c, cname, msgs, recent in rows:
        lines = [_fmt_msg(m, R) for m in msgs if ap.classify(m["text"]) not in ("empty",)]
        if not lines:
            continue
        head = ("%s 的对话 - %s - %s:" % (today, kind_c, cname) if not recent
                else "近期消息(截至%s，微信归档无具体日期) - %s - %s:" % (today, kind_c, cname))
        piece = head + "\n" + "\n".join(lines) + "\n"
        if total + len(piece) > max_chars:
            break
        parts.append(piece)
        total += len(piece)
    return "\n".join(parts)


def today_digest(platform):
    """本地统计今天每个会话的消息数与各发送者条数（用统一解析层，准确）。

    旧实现的正则对三种真实行格式全都匹配不到，导致 senders_today 永远为空、
    回答永远是"今天没有任何人给你发过私信"。这里改成走 archive_parse。
    """
    rows = _today_scan(platform)
    if not rows:
        return ""
    R = vix.resolver()
    today = datetime.now().strftime("%Y-%m-%d")
    my_qq = (cfg.qq_uin() or "").strip()
    stat = []
    senders_today = {}
    for kind_c, cname, msgs, recent in rows:
        per = {}
        for m in msgs:
            if ap.classify(m["text"]) == "empty":
                continue
            nm, qq, mine = R.sender(m["sender_raw"])
            if mine or (my_qq and qq == my_qq):
                per["我"] = per.get("我", 0) + 1
                continue
            per[nm] = per.get(nm, 0) + 1
            if kind_c == "私聊" and not recent:
                senders_today[nm] = senders_today.get(nm, 0) + 1
        if per:
            stat.append((cname, kind_c, per, len(msgs), recent))

    out = ["今日(%s)消息统计(由本地程序逐条解析统计,准确可靠):" % today]
    if senders_today:
        out.append("★结论★ 今天给你发过私信的人: "
                   + "、".join("%s(%d条)" % (k, v) for k, v in
                               sorted(senders_today.items(), key=lambda x: -x[1]))
                   + "。下面是分会话明细:")
    elif any(not r for _c, _k, _p, _n, r in stat):
        out.append("★结论★ 今天没有任何人给你发过私信。")
    else:
        out.append("★结论★ 今天私聊无新消息（微信归档无具体日期,只能给整月统计）。")
    for cname, kind_c, per, n, recent in stat:
        detail = "、".join("%s %d条" % (k, v) for k, v in
                          sorted(per.items(), key=lambda x: -x[1]))
        out.append("- [%s] %s%s: 共%d条 — %s"
                   % (kind_c, cname, "（近期）" if recent else "", n, detail))
    return "\n".join(out)


def gather_conv_tails(platform, hits, n_convs=3, max_chars=5000, max_lines=25):
    """从关键词命中的会话中,取每个会话最新月份文件的末尾消息(会话近况)。"""
    roots = scan_roots(platform)
    if not roots or not hits:
        return ""
    conv_order = []
    conv_files = {}
    for h in hits:
        rel = (h.get("来源") or "").replace("[增量] ", "")
        conv = os.path.dirname(rel)
        if not conv or conv == ".":
            continue
        if conv not in conv_files:
            conv_files[conv] = []
            conv_order.append(conv)
        conv_files[conv].append(rel)
    parts = []
    total = 0
    for conv in conv_order[:n_convs]:
        # 在各根下定位该会话目录
        best = None  # (ym, path, root)
        for r in roots:
            cdir = os.path.join(r, conv)
            if not os.path.isdir(cdir):
                continue
            for f in glob.glob(os.path.join(cdir, "*.md")):
                ym = _parse_ym(f)
                if ym and (best is None or ym > best[0]):
                    best = (ym, f, r)
        if not best:
            continue
        _, f, r = best
        tail = _tail_of(f, max_lines)
        if not tail:
            continue
        piece = "会话近况 %s 最新(%d-%02d)月末:\n%s\n" % (conv, best[0][0], best[0][1], tail)
        if total + len(piece) > max_chars:
            break
        parts.append(piece)
        total += len(piece)
    return "\n".join(parts)


def load_uid_map():
    """uid→昵称映射(由 sync_qq 同步时写入 uid_map.json)。"""
    p = os.path.join(cfg.BASE, "uid_map.json")
    try:
        with open(p, encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def load_group_map():
    """群号→群名称映射(由 sync_qq 同步时写入 group_map.json)。"""
    p = os.path.join(cfg.BASE, "group_map.json")
    try:
        with open(p, encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def _load_json_base(name):
    p = os.path.join(cfg.BASE, name)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def load_c2c_peers():
    """私聊对象索引（sync_qq 写 c2c_peers.json）。

    返回 None 表示索引还没生成，联系人页回退旧行为（显示 uid_map 全部）。
    """
    return _load_json_base("c2c_peers.json")


_GROUP_MEMBERS_CACHE = {"path": "", "mtime": None, "data": None}


def load_group_members():
    """群号→成员列表（sync_qq 写 group_members.json），按 mtime 缓存。"""
    p = os.path.join(cfg.BASE, "group_members.json")
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return None
    c = _GROUP_MEMBERS_CACHE
    if c["path"] == p and c["mtime"] == mt:
        return c["data"]
    data = _load_json_base("group_members.json")
    c.update({"path": p, "mtime": mt, "data": data})
    return data


# ---------------------------------------------------------------- 联系人

def _conv_dirs(sub):
    """归档某子目录下的会话目录: {编号(QQ号/群号): [目录名,...]}。"""
    root = os.path.join(ROOTS.get("QQ", ""), sub)
    out = {}
    if os.path.isdir(root):
        for d in sorted(os.listdir(root)):
            if not os.path.isdir(os.path.join(root, d)):
                continue
            m = re.search(r"(\d{5,})", d)
            if m:
                out.setdefault(m.group(1), []).append(d)
    return out


def _dir_coverage(sub, dirname):
    """会话目录的月份覆盖(只看文件名,不读内容,毫秒级)。"""
    root = os.path.join(ROOTS.get("QQ", ""), sub, dirname)
    months = []
    if os.path.isdir(root):
        for f in os.listdir(root):
            m = re.match(r"(\d{4}-\d{2})\.md$", f)
            if m:
                months.append(m.group(1))
    months.sort()
    return {"最早": months[0] if months else "",
            "最晚": months[-1] if months else "",
            "月份数": len(months)}


def api_contacts(keyword=""):
    """联系人列表：只保留真正私聊过的人；群成员在各群详情里单独查看。

    数据源: uid_map.json(昵称/备注/QQ) + c2c_peers.json(私聊对象)
    + group_map.json(群名) + group_members.json(群成员数) + 归档目录名。
    keyword 过滤 昵称/备注/QQ号,前端也做了实时过滤,这里兜底大数据量场景。
    """
    kw = (keyword or "").strip().lower()
    priv = _conv_dirs("私聊")
    groups = _conv_dirs("群聊")
    gmap = load_group_map()
    peers = load_c2c_peers()
    peer_uids = {str(x) for x in ((peers or {}).get("uids") or [])}
    peer_qqs = {str(x) for x in ((peers or {}).get("qqs") or [])}
    gm = load_group_members()
    gmembers = (gm or {}).get("groups") or {}
    # 目录名 → 会话身份键（私聊/QQ123），必须和索引里的一致，否则"打开完整对话"打不开
    R = vix.resolver()
    try:
        R.learn_from_dirs(scan_roots("QQ"))
    except Exception:
        pass

    def _key(dirs, kind):
        return R.conv_key_of_dir(dirs[0], kind) if dirs else ""

    contacts = []
    seen_qq = set()
    for uid, info in (load_uid_map() or {}).items():
        if not isinstance(info, dict):
            continue
        qq = str(info.get("qq") or "").strip()
        nick = str(info.get("nick_raw") or info.get("nick") or "").strip()
        remark = str(info.get("remark") or "").strip()
        # 只保留「真正私聊过」的人：c2c 对方 / 归档私聊目录。
        # c2c_peers.json 还没生成时回退旧行为，避免升级后联系人页突然空白。
        if peers is not None:
            is_private = (uid in peer_uids or (qq and qq in peer_qqs)
                          or (qq and qq in priv))
            if not is_private:
                continue
        display = remark or nick or ("QQ" + qq if qq else uid[:12])
        # 有些昵称本身就是 "(2046716658)" 这种空壳，显示成"未命名"更诚实
        if re.fullmatch(r"[（(]\s*\d{5,12}\s*[)）]", display.strip()):
            display = "QQ" + qq if qq else "未命名"
        dirs = priv.get(qq, [])
        item = {"uid": uid, "qq": qq, "昵称": nick, "备注": remark,
                "显示名": display, "会话": dirs, "会话键": _key(dirs, "私聊"),
                "覆盖": _dir_coverage("私聊", dirs[0]) if dirs else {}}
        if kw and kw not in display.lower() and kw not in nick.lower() \
                and kw not in remark.lower() and kw not in qq:
            continue
        contacts.append(item)
        if qq:
            seen_qq.add(qq)

    # 有私聊归档、但 uid_map 中没有的历史联系人（换过库/只在旧归档里）
    if peers is not None:
        for qq, dirs in sorted(priv.items()):
            if qq in seen_qq:
                continue
            dn = dirs[0]
            m = re.match(r"^(.*?)[（(]\d{5,12}[)）]", dn)
            nick = m.group(1).strip() if m else ""
            if not nick and not dn.startswith("QQ"):
                nick = dn.split("_")[0].strip()
            display = nick or ("QQ" + qq)
            if kw and kw not in display.lower() and kw not in qq:
                continue
            contacts.append({
                "uid": "", "qq": qq, "昵称": nick, "备注": "",
                "显示名": display, "会话": dirs,
                "会话键": _key(dirs, "私聊"),
                "覆盖": _dir_coverage("私聊", dirs[0]),
            })
    # 有归档记录的排前面,再按显示名排序
    contacts.sort(key=lambda c: (not c["会话"], c["显示名"]))

    glist = []
    seen_g = set()
    for gnum, gname in gmap.items():
        dirs = groups.get(str(gnum), [])
        item = {"群号": str(gnum), "群名": str(gname), "会话": dirs,
                "会话键": _key(dirs, "群聊"),
                "成员数": len(gmembers.get(str(gnum)) or []),
                "覆盖": _dir_coverage("群聊", dirs[0]) if dirs else {}}
        if kw and kw not in str(gname).lower() and kw not in str(gnum):
            continue
        glist.append(item)
        seen_g.add(str(gnum))
    for gnum in sorted(set(groups) | set(gmembers)):
        if gnum in seen_g:
            continue
        if kw and kw not in gnum:
            continue
        glist.append({"群号": gnum, "群名": gmap.get(gnum, ""),
                      "会话": groups.get(gnum, []),
                      "会话键": _key(groups.get(gnum, []), "群聊"),
                      "成员数": len(gmembers.get(gnum) or []),
                      "覆盖": _dir_coverage("群聊", groups[gnum][0]) if groups.get(gnum) else {}})
    glist.sort(key=lambda g: (not g["会话"], g["群名"] or g["群号"]))
    return {"联系人": contacts, "群聊": glist,
            "索引": {"私聊": peers is not None, "群成员": gm is not None},
            "总数": {"联系人": len(contacts), "群聊": len(glist)}}


def api_group_members(gnum):
    """某个群的成员列表；索引未生成时提示先同步一次。"""
    gnum = str(gnum or "").strip()
    gm = load_group_members()
    if gm is None:
        return {"错误": "群成员索引尚未生成，请先点一次「同步 QQ」",
                "群号": gnum, "群名": "", "成员": [], "总数": 0}
    members = (gm.get("groups") or {}).get(gnum) or []
    return {"群号": gnum, "群名": load_group_map().get(gnum, ""),
            "成员": members, "总数": len(members)}


def translate_uids(text):
    """把文本里的 u_xxx 编码替换为 昵称(QQ号),旧增量数据也能看懂。

    旧文件里发送者被截断到 16 字符,所以完整 uid 的各种前缀也要匹配。
    """
    um = load_uid_map()
    if not um:
        return text
    for uid, info in sorted(um.items(), key=lambda kv: -len(kv[0])):
        if not isinstance(info, dict) or not uid:
            continue
        nick = (info.get("nick") or "").strip()
        qq = (info.get("qq") or "").strip()
        if nick and qq:
            label = f"{nick}({qq})"
        elif nick:
            label = nick
        elif qq:
            label = f"QQ{qq}"
        else:
            continue
        # 完整 uid 与历史截断前缀(旧格式截到16字符)都替换
        for n in sorted({len(uid), 24, 20, 18, 16}, reverse=True):
            if n < 12 or n > len(uid):
                continue
            pre = uid[:n]
            if pre in text:
                text = text.replace(pre, label)
    return text


def mark_self(text):
    """把上下文里本人发送者标注改成 [我·用户本人]，让模型无需推理即可区分。

    免费小模型做“QQ号比对”归类不可靠，用显式标记消除歧义。
    """
    my_qq = (cfg.qq_uin() or "").strip()
    if not my_qq:
        return text
    # 只替换带方括号的发送者标注 [昵称(本人QQ)],避免误伤路径等普通文本
    my_qq_e = re.escape(my_qq)
    text = re.sub(r"\[([^\[\]]*?)\(" + my_qq_e + r"\)\]",
                  lambda m: "[我·用户本人(原:%s)]" % m.group(1).strip(), text)
    return text


def _auto_chunks(user_text):
    """检索词留空时,从问题中提取内容块作检索词(按停用词切分,保留专名/名词)。
    例: “我和杜可欣的关系怎么样” → ["杜可欣", "关系"]
        “今天有谁给我发消息了” → ["发消息"]
    """
    t = re.sub(r"[？?!。，,．.、\s：:；;\"'（）()《》【】]+", " ", user_text)
    stop = (r"(?:今天|昨天|前天|最近|这几天|请问|帮我|告诉我|看看|看一下|查一下|查询|"
            r"根据|一下|注意|需要|应该|可以|现在|有没有|多少|几点|哪些|什么|怎么|如何|"
            r"为什么|怎么样|有谁|谁|给我|发了|消息吗|我|你|他|她|它|们|吗|呢|吧|了|"
            r"的|是|有|要|和|跟|与|在|上|里|中|都|很|也|就|还|过|着|这|那|个|聊|说)")
    parts = re.split(stop, t)
    out = []
    for p in parts:
        p = p.strip()
        if len(p) >= 2 and p not in out:
            out.append(p)
    return out[:4]


def gather_context_multi(platform, queries, max_chars=12000, max_hits=40):
    """多检索词归档检索（走索引），拼成"时间 发送者: 内容"的人话片段。

    旧实现把 Markdown 原始行（含 ** 和行号）塞给模型，模型要自己解析格式，
    弱模型经常读错；这里直接给结构化后的文本。
    """
    merged, order, seen = {}, [], set()
    for q in queries:
        r = vix.search(platform, q, limit=30)
        if r.get("错误"):
            continue
        for h in r.get("结果", []):
            key = h["id"]
            if key in seen:
                continue
            seen.add(key)
            order.append(h)
            if len(order) >= max_hits:
                break
        if len(order) >= max_hits:
            break
    parts, total = [], 0
    for h in order:
        who = "我" if h["我"] else h["发送者"]
        body = h["显示"]
        if h["引用"]:
            body = "（回复「%s」）%s" % (h["引用"], body)
        piece = "[%s %s %s] %s: %s\n" % (
            h["会话名"], h["日期"].replace("-00", ""), h["时间"][:5], who, body)
        if total + len(piece) > max_chars:
            break
        parts.append(piece)
        total += len(piece)
    return "".join(parts), order


# ---------------------------------------------------------------- 对话

def call_llm(messages, provider, temperature=0.3):
    """通用 OpenAI 兼容调用:支持智谱/DeepSeek/Kimi/OpenAI 等云端 API,
    以及 Ollama(http://127.0.0.1:11434)、LM Studio(http://127.0.0.1:1234) 等本地服务。
    本地服务 key 留空时不发送 Authorization 头。"""
    api = provider.get("api")
    model = provider.get("model")
    key = provider.get("key") or ""
    if not api or not model:
        raise RuntimeError("服务接口地址或模型名未配置")
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(api, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read())
    return d["choices"][0]["message"]["content"]


def api_test_provider(payload):
    """测试一个模型服务配置(用表单里的值,不需要先保存)。"""
    provider = {
        "api": (payload.get("api") or "").strip(),
        "key": (payload.get("key") or "").strip(),
        "model": (payload.get("model") or "").strip(),
    }
    try:
        reply = call_llm(
            [{"role": "user", "content": "请只回复两个字：正常"}],
            provider, temperature=0.1)
        return {"成功": True, "回复": (reply or "")[:100],
                "服务": "%s @ %s" % (provider["model"], provider["api"])}
    except Exception as e:
        return {"成功": False, "错误": str(e)[:300]}


DEFAULT_SYSTEM_PROMPT = (
    "你是用户的私人聊天档案助手，用简体中文回答。"
    "只依据提供的信息，不编造。用户提供的聊天片段中“我”代表用户本人。"
)

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _time_anchor():
    """当前真实时间锚点，防止模型把归档里的历史日期当成“今天”。"""
    now = datetime.now()
    return ("当前真实时间: %d-%02d-%02d %02d:%02d %s。"
            % (now.year, now.month, now.day, now.hour, now.minute,
               WEEKDAYS[now.weekday()]))


def _archive_rules():
    return ("归档内容是历史记录，其中出现的日期不代表今天；"
            "判断“今天/最近”一律以当前真实时间为准。"
            "小节标题带今天日期的内容就是今天的消息，可直接引用。")


def do_chat(user_text, history, use_context, context_query, platform, provider_idx=None):
    """自由对话: 任何送出去的文本强制脱敏。返回 dict 含回复与元信息。

    上下文策略(勾选“附带归档上下文”时):
      1. 填了检索词 → 按关键词检索归档;
      2. 留空 → 用问题本身自动检索,命中不足时追加归档最近消息。
    """
    p = cfg.provider_at(provider_idx if provider_idx is not None
                        else cfg.active_provider())

    # 组装上下文(来自本地归档)
    context = ""
    ctx_note = ""
    n_kw = 0
    hits = []
    if use_context:
        pieces = []
        q = (context_query or "").strip()
        explicit = bool(q)
        chunks = [q] if explicit else _auto_chunks(user_text)
        raw, hits = gather_context_multi(platform, chunks) if chunks else ("", [])
        n_kw = len(hits)
        qdesc = "、".join("「%s」" % c for c in chunks) if chunks else ""
        # 本地统计摘要放最前:弱模型直接读结论,不必自己从原文归纳
        digest = today_digest(platform)
        if digest:
            pieces.append(sanitize.sanitize_text(digest))
        # 今天各会话的全部消息放最前面:回答“今天/谁发消息”的主力证据,
        # 且不能被大块历史命中片段淹没(小模型注意力有限)
        td = today_lines(platform)
        if td:
            pieces.append("以下是归档中今天(%s)各会话的消息原文(已脱敏)，"
                          "以“今天(%s)的对话”开头的段落里的消息都是今天发的："
                          % (datetime.now().strftime("%Y-%m-%d"),
                             datetime.now().strftime("%Y-%m-%d"))
                          + "\n%s" % sanitize.sanitize_text(td))
        # 问“今天/最近”类问题时:只发 摘要+今日原文。实测 glm-4-flash 在
        # 14k 字符混杂上下文里会读错归属,精简后 100% 正确;历史检索只在
        # 关键词类问题上启用。
        today_q = bool(re.search(r"今天|今日|最近|昨天|刚刚|新消息|最新", user_text))
        if today_q and digest:
            if raw:
                pass  # 今日问题不需要历史命中
            conv_raw = ""
            tail = ""
            want_recent = False
        else:
            if raw:
                pieces.append("以下是从用户本地聊天归档按关键词 %s 检索到的历史片段(已脱敏):\n%s"
                              % (qdesc, sanitize.sanitize_text(raw)))
            # 会话近况: 命中会话的最新月份末尾,回答“某人最近有没有发消息”
            conv_raw = gather_conv_tails(platform, hits)
            if conv_raw:
                pieces.append("以下是上述会话的最新月份文件末尾消息(已脱敏),代表各会话目前进行到哪:\n%s"
                              % sanitize.sanitize_text(conv_raw))
            # 全归档当月近况: 手动模式或命中不足时也附
            want_recent = (not explicit) or n_kw < 3
            if want_recent:
                tail = recent_lines(platform)
                if tail:
                    pieces.append("以下是归档中当月的部分会话末尾消息(已脱敏),可用于回答“最近”类问题:\n%s"
                                  % sanitize.sanitize_text(tail))
        if pieces:
            context = mark_self(translate_uids("\n\n".join(pieces)))
            ctx_note = (("关键词 %s 命中 %d 条" % (qdesc, n_kw)) if raw else "关键词无命中")
            extras = []
            if td:
                extras.append("今日消息")
            if conv_raw:
                extras.append("会话近况")
            if want_recent and tail:
                extras.append("当月近况")
            if extras:
                ctx_note += " + " + "+".join(extras)

    # 强制脱敏: 用户输入 + 全部历史,一律过 sanitize
    safe_user = sanitize.sanitize_text(user_text)
    safe_history = [
        {"role": m["role"], "content": sanitize.sanitize_text(m["content"])}
        for m in history
        if m.get("role") in ("user", "assistant")
    ]

    # 提示词: 用户自定义(设置页可改)或内置默认;始终追加时间锚点与归档规则
    base = (cfg.chat_system_prompt().strip() or DEFAULT_SYSTEM_PROMPT)
    time_anchor = _time_anchor()
    my_qq = (cfg.qq_uin() or "").strip()
    self_note = ("\n标为 [我·用户本人] 的行是用户自己发的消息，"
                 "其他标注都是对方发的；引用消息时保留发送者标注。")
    if context:
        # 精简原则:弱模型(glm-4-flash)对多条规则指令会"绕晕",只保留最关键的三条
        system = (
            base + self_note + "\n" + time_anchor + "\n"
            "下面提供的内容就是全部事实，直接依据它回答，引用时保留发送者标注。"
        )
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": context},
                {"role": "assistant", "content": "我已阅读这些本地归档内容，会依据它们回答你的问题。"}]
    else:
        system = base + "\n" + time_anchor + "\n" + _archive_rules()
        msgs = [{"role": "system", "content": system}]
    msgs.extend(safe_history)
    msgs.append({"role": "user", "content": safe_user})

    try:
        reply = call_llm(msgs, p)
    except Exception as e:
        return {"回复": "[调用失败] " + str(e), "模型": p.get("model") or "",
                "服务": p.get("name") or "", "片段": 0, "上下文": "", "参考": []}
    # 把"这次到底读了哪几条"回传给界面：模型胡说八道时用户能当场核对
    refs = []
    for h in (hits or [])[:20]:
        refs.append({"id": h.get("id", 0), "平台": h.get("平台", ""),
                     "会话": h.get("会话", ""), "会话名": h.get("会话名", ""),
                     "日期": h.get("日期", ""), "时间": (h.get("时间") or "")[:5],
                     "发送者": "我" if h.get("我") else h.get("发送者", ""),
                     "显示": (h.get("显示") or "")[:140]})
    return {"回复": reply, "模型": p.get("model") or "",
            "服务": p.get("name") or "", "片段": n_kw,
            "上下文": ctx_note, "参考": refs}


# ---------------------------------------------------------------- 密钥状态

def last_sync_time(log_path):
    try:
        with open(log_path, encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
        for line in reversed(lines):
            m = re.search(r"\[(20\d\d-\d\d-\d\d \d\d:\d\d:\d\d)\]", line)
            if m:
                return m.group(1)
    except Exception:
        pass
    return "(无记录)"


def _wx_key_configured():
    """微信 all_keys.json 是否至少有一条带 enc_key 的记录。"""
    try:
        p = cfg.all_keys_file()
        if not p or not os.path.exists(p):
            return False
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        for v in (d or {}).values():
            if isinstance(v, dict) and (v.get("enc_key") or v.get("key")):
                return True
    except Exception:
        pass
    return False


def _model_key_configured():
    try:
        if load_glm_key():
            return True
        for p in cfg.providers():
            if isinstance(p, dict) and p.get("key"):
                return True
    except Exception:
        pass
    return False


def key_flags():
    st = key_status()
    return {"QQ": bool(st.get("QQ密钥已配置")),
            "微信": bool(st.get("微信密钥已配置")),
            "模型": bool(st.get("模型密钥已配置"))}


def key_status():
    qq_key = ""
    if os.path.exists(QQ_KEYFILE):
        with open(QQ_KEYFILE, encoding="utf-8", errors="ignore") as f:
            qq_key = f.read().strip()
    lib_exists = os.path.exists(QQ_LIB)
    lib_mtime = ""
    if lib_exists:
        lib_mtime = datetime.fromtimestamp(os.path.getmtime(QQ_LIB)).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "QQ密钥": mask_key(qq_key),
        "QQ密钥已配置": bool(qq_key),
        "QQ密钥文件": QQ_KEYFILE or "(未配置)",
        "活跃库存在": lib_exists,
        "活跃库修改时间": lib_mtime or "-",
        "QQ最后同步": last_sync_time(QQ_LOG),
        "微信最后同步": last_sync_time(WX_LOG),
        "GLM密钥": mask_key(load_glm_key()),
        "GLM密钥已配置": bool(load_glm_key()),
        "微信密钥已配置": _wx_key_configured(),
        "模型密钥已配置": _model_key_configured(),
        "配置说明": "" if cfg.is_configured() else cfg.setup_hint(),
    }


def test_qq_key():
    """复用 sync_qq.py --dry-run 验证密钥,不重复造轮子。"""
    sync_script = os.path.join(BASE, "sync_qq.py")
    if not os.path.exists(sync_script):
        return {"成功": False,
                "输出": "同步脚本未包含在本次发布中;密钥测试需在本地完整部署环境使用。",
                "日志": ""}
    try:
        proc = subprocess.run(
            [sys.executable, sync_script, "--dry-run"],
            capture_output=True, text=True, timeout=180, cwd=BASE,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        # 成功后读取日志末段判断
        tail = ""
        if os.path.exists(QQ_LOG):
            with open(QQ_LOG, encoding="utf-8", errors="ignore") as f:
                tail = "\n".join(f.read().splitlines()[-12:])
        ok = ("解析完成" in tail or "无新消息" in tail or "新消息:" in tail) and ("ERROR" not in tail[-500:])
        return {"成功": ok, "输出": out[-1000:], "日志": tail}
    except Exception as e:
        return {"成功": False, "输出": "测试异常: " + str(e), "日志": ""}


# ---------------------------------------------------------------- 同步 / 密钥与索引

# ---- 后台任务管理器（同步/抓密钥耗时较长,用线程跑,前端轮询进度）----
import threading
import importlib

JOBS = {}
_JOB_LOCK = threading.Lock()
_JOB_SEQ = [0]


def _job_log(jid, line):
    job = JOBS.get(jid)
    if job is not None:
        job["输出"].append(str(line))
        if len(job["输出"]) > 400:
            del job["输出"][:len(job["输出"]) - 400]


def _explain_sync_error(name, lines, status=""):
    """把同步日志里的技术错误翻成用户能看懂的原因和下一步。"""
    text = str(status or "") + "\n" + "\n".join(str(x) for x in (lines or [])[-80:])
    low = text.lower()

    def has(*words):
        return any(w.lower() in low for w in words)

    if has("密钥失效", "密钥错误", "hmac", "decrypt fail", "解密失败"):
        return "密钥和聊天数据库对不上，密钥可能过期了。下一步：到「密钥与索引」重新填写或抓取密钥，再同步一次。"
    if has("快照完成: 0 个文件", "快照完成：0 个文件"):
        return "没找到聊天数据库文件。下一步：到「设置 → 文件位置」检查 QQ / 微信的原始数据位置；确认电脑版 QQ / 微信登录过，并且没有清理过聊天数据。"
    if has("找不到输入文件", "no such file") and has("nt_msg", ".db", "db_storage", "聊天数据库"):
        return "原始聊天数据的位置不对。下一步：到「设置 → 文件位置」重新选择 QQ / 微信的数据文件夹，再同步。"
    if has("key") and has("不存在", "not found", "no such file", "missing"):
        return "没有找到密钥文件。下一步：到「密钥与索引」填好密钥；微信可以点「一键抓取微信密钥」。"
    if has("weixin.exe", "微信未登录", "微信进程", "微信未运行"):
        return "没有找到正在运行的微信。下一步：先登录电脑微信，保持窗口打开，再点同步。"
    if has("nt_msg", "db_storage", "聊天数据库", "数据目录", "原始数据") and has(
            "不存在", "找不到", "not found", "no such file"):
        return "找不到原始聊天数据。下一步：到「设置 → 文件位置」检查 QQ / 微信的原始数据位置是否填对。"
    if has("permissionerror", "拒绝访问", "access is denied", "being used", "locked", "占用"):
        return "聊天数据库正被 QQ 或微信占用。下一步：先把 QQ 或微信完全退出，等几秒再同步。"
    if has("未随程序打包", "no module named", "缺少解密工具", "工具缺失"):
        return "这个版本缺少解密工具。下一步：换用完整版，或把 tools 文件夹放回程序旁边。"
    if has("timeout", "timed out", "超时"):
        return "同步超时了。下一步：关掉软件重开再试一次；还不行就点「查看日志」把最后几行发出来。"
    tail = ""
    for ln in reversed((lines or [])):
        s = str(ln).strip()
        if s and not s.startswith("="):
            tail = s[:120]
            break
    if tail:
        return "同步没完成。日志最后一行是：" + tail + "。下一步：点「查看日志」把最后几行发出来。"
    return "同步没完成，但没有留下明确原因。下一步：点「查看日志」，把最后几行发出来。"


def start_job(name, fn):
    """启动后台任务线程;fn(log_fn) 执行完返回 int 退出码。"""
    t0 = time.time()
    with _JOB_LOCK:
        _JOB_SEQ[0] += 1
        jid = str(_JOB_SEQ[0])
        JOBS[jid] = {
            "id": jid, "名称": name, "状态": "运行中",
            "输出": [], "退出码": None,
            "进度": {"percent": 0, "stage": "准备中", "at": t0},
            "开始": datetime.now().strftime("%H:%M:%S"), "开始秒": t0,
            "错误说明": "",
        }
    def run():
        job = JOBS[jid]
        def log_fn(line):
            _job_log(jid, line)
        def set_progress(pct, stage):
            with _JOB_LOCK:
                job["进度"] = {"percent": max(0, min(100, int(pct))),
                               "stage": str(stage), "at": time.time()}
        log_fn.set_progress = set_progress
        try:
            code = fn(log_fn)
            job["退出码"] = code
            job["状态"] = "完成" if code == 0 else "失败(退出码%d)" % code
        except Exception as e:
            job["退出码"] = -1
            job["状态"] = "异常: %s" % e
        job["结束"] = datetime.now().strftime("%H:%M:%S")
        if "同步" in str(name):
            if job.get("退出码") not in (None, 0):
                job["错误说明"] = _explain_sync_error(name, job.get("输出"), job.get("状态"))
                _SCHED["last_sync_error"] = job["错误说明"]
            else:
                _SCHED["last_sync_error"] = ""
    threading.Thread(target=run, daemon=True).start()
    return dict(JOBS[jid])


def _tool_src_dirs():
    """两个解密工具的 src 目录（开发:BASE/tools;打包:RES_DIR/tools）。"""
    roots = [BASE]
    if cfg.RES_DIR and cfg.RES_DIR != BASE:
        roots.append(cfg.RES_DIR)
    out = []
    for root in roots:
        for sub in ("qq-export-helper", "wechat4-db-decrypt"):
            p = os.path.join(root, "tools", sub, "src")
            if os.path.isdir(p):
                out.append(p)
    return out


def _ensure_tool_paths():
    for p in _tool_src_dirs():
        if p not in sys.path:
            sys.path.insert(0, p)
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)


def _run_sync(target, dry, log_fn):
    """进程内执行增量同步（exe 中无法再起 python 子进程）。返回退出码。"""
    mod_name = "sync_qq" if target == "qq" else "sync_wechat"
    PROGRESS_TAG = "@@PROGRESS@@ "
    _ensure_tool_paths()
    mod = importlib.import_module(mod_name)
    old_argv, sys.argv = sys.argv, [mod_name] + (["--dry-run"] if dry else [])
    old_stdout = sys.stdout

    class _Tee:
        def write(self, s):
            for ln in str(s).splitlines():
                if not ln.strip():
                    continue
                if ln.startswith(PROGRESS_TAG):
                    pct, _, stage = ln[len(PROGRESS_TAG):].partition("|")
                    setp = getattr(log_fn, "set_progress", None)
                    if setp:
                        try:
                            setp(int(pct), stage)
                        except ValueError:
                            pass
                    continue
                log_fn(ln)
            return len(s)

        def flush(self):
            pass

    try:
        sys.stdout = _Tee()
        code = mod.main()
    finally:
        sys.stdout = old_stdout
        sys.argv = old_argv
    return code or 0


def api_start_sync(target, dry):
    if target not in ("qq", "wechat"):
        return {"错误": "target 必须是 qq 或 wechat"}
    label = ("QQ" if target == "qq" else "微信") + (" 同步(试运行)" if dry else " 增量同步")
    return start_job(label, lambda log_fn: _run_sync(target, dry, log_fn))


def api_job(job_id):
    job = JOBS.get(str(job_id))
    if not job:
        return {"错误": "任务不存在"}
    return {"id": job["id"], "名称": job["名称"], "状态": job["状态"],
            "退出码": job["退出码"], "输出": job["输出"][-60:],
            "开始秒": job.get("开始秒"), "错误说明": job.get("错误说明", ""),
            "进度": dict(job.get("进度") or {"percent": 0, "stage": ""})}


# ---- 自动同步调度（软件运行期间生效；关闭软件即停止）----
_SCHED = {
    "thread": None,
    "started_at": None,
    "startup_pending": True,
    "last_auto": None,
    "last_targets": [],
    "next_run": None,
    "last_error": "",
}


def _sync_cfg_safe():
    try:
        return cfg.sync_cfg()
    except Exception:
        return {"enabled": False, "mode": "interval", "interval_minutes": 60,
                "daily_time": "00:30", "targets": ["qq"], "on_startup": False,
                "rebuild_index": False}


def _sync_running():
    """手动或自动同步正在跑时返回 True，避免重复触发。"""
    with _JOB_LOCK:
        for j in JOBS.values():
            if j.get("状态") == "运行中" and "同步" in str(j.get("名称", "")):
                return True
    return False


def _run_auto_all(targets, rebuild, log_fn):
    """自动同步串行执行 QQ/微信，再按需增量更新索引。"""
    for t in targets:
        log_fn("自动同步开始: " + ("QQ" if t == "qq" else "微信"))
        code = _run_sync(t, False, log_fn)
        if code:
            return code
    if rebuild:
        log_fn("同步完成，增量更新搜索索引 …")
        try:
            _build_index_job(False, log_fn)
        except Exception as e:
            log_fn("索引更新失败: %s" % e)
    log_fn("自动同步结束")
    return 0


def _trigger_auto_sync(s, now):
    if _sync_running():
        return False
    targets = list(s.get("targets") or [])
    if not targets:
        return False
    _SCHED["last_auto"] = now
    _SCHED["last_targets"] = targets
    label = "自动同步（" + "+".join("QQ" if t == "qq" else "微信" for t in targets) + "）"
    start_job(label, lambda log_fn: _run_auto_all(
        targets, s.get("rebuild_index", True), log_fn))
    return True


def _auto_sync_tick(now=None):
    """调度心跳（15 秒一次）；返回本轮是否触发同步。"""
    now = now or datetime.now()
    s = _sync_cfg_safe()
    if not s["enabled"]:
        _SCHED["next_run"] = None
        return False
    started = _SCHED.get("started_at")
    if _SCHED.get("startup_pending"):
        if not s.get("on_startup"):
            _SCHED["startup_pending"] = False
        elif started and (now - started).total_seconds() >= 60:
            _SCHED["startup_pending"] = False
            if _trigger_auto_sync(s, now):
                _SCHED["next_run"] = None
                return True
    if s["mode"] == "interval":
        base = _SCHED.get("last_auto") or started or now
        nxt = base + timedelta(minutes=s["interval_minutes"])
    else:
        hh, mm = [int(x) for x in s["daily_time"].split(":")]
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
    _SCHED["next_run"] = nxt
    if now >= nxt:
        if _trigger_auto_sync(s, now):
            _SCHED["next_run"] = None
            return True
        _SCHED["next_run"] = now + timedelta(minutes=5)
    return False


def start_sync_scheduler():
    """幂等启动调度线程；源码模式与窗口模式共用。"""
    if _SCHED.get("thread"):
        return
    _SCHED["started_at"] = datetime.now()
    _SCHED["startup_pending"] = True

    def loop():
        while True:
            try:
                _auto_sync_tick()
                _SCHED["last_error"] = ""
            except Exception as e:
                _SCHED["last_error"] = str(e)
            time.sleep(15)

    t = threading.Thread(target=loop, daemon=True)
    _SCHED["thread"] = t
    t.start()


def _fmt_dt(d):
    return d.strftime("%Y-%m-%d %H:%M:%S") if isinstance(d, datetime) else ""


def api_sync_status():
    """同步页状态：配置、上次/下次、当前任务。"""
    s = _sync_cfg_safe()
    running = None
    with _JOB_LOCK:
        for j in JOBS.values():
            if j.get("状态") == "运行中" and "同步" in str(j.get("名称", "")):
                running = {"id": j["id"], "名称": j["名称"],
                           "进度": dict(j.get("进度") or {}),
                           "开始秒": j.get("开始秒"),
                           "最新": (j.get("输出") or [""])[-1]}
                break
    return {
        "配置": s,
        "上次QQ": last_sync_time(cfg.qq_log()),
        "上次微信": last_sync_time(cfg.wechat_log()),
        "自动上次": _fmt_dt(_SCHED.get("last_auto")),
        "下次自动": _fmt_dt(_SCHED.get("next_run")),
        "启动时间": _fmt_dt(_SCHED.get("started_at")),
        "运行中": running,
        "错误": str(_SCHED.get("last_error") or ""),
        "最近失败": str(_SCHED.get("last_sync_error") or ""),
    }


# ---- 微信密钥抓取（只读扫描 Weixin.exe,可复现）----

def _run_wx_hunt(log_fn):
    """进程内执行 wxexport.keys.hunt + map,生成 all_keys.json。"""
    wx_src = None
    for p in _tool_src_dirs():
        if "wechat4-db-decrypt" in p:
            wx_src = p
    if not wx_src:
        raise RuntimeError("微信解密工具未随程序打包")
    _ensure_tool_paths()
    import io, contextlib
    from wxexport.keys import hunt as wx_hunt
    from wxexport.keys import map as wx_map
    old_argv = sys.argv
    old_stdout = sys.stdout

    class _Tee:
        def write(self, s):
            for ln in str(s).splitlines():
                if ln.strip():
                    log_fn(ln)
            return len(s)

        def flush(self):
            pass

    try:
        sys.stdout = _Tee()
        argv = ["hunt"]
        # 测试包通常不带 config.json：自动找本机微信数据目录，避免卡在“未配置”
        cfg_file = os.path.join(os.path.dirname(wx_src), "config.json")
        if not os.path.exists(cfg_file):
            try:
                from wxexport.config import auto_detect_db_dir
                db_dir = auto_detect_db_dir()
            except Exception as e:
                db_dir = None
                log_fn("自动检测微信数据目录失败: %s" % e)
            if not db_dir:
                raise RuntimeError("未检测到微信数据目录，请先登录电脑版微信再试")
            argv += ["--db-dir", str(db_dir)]
            log_fn("自动检测到微信数据目录: %s" % db_dir)
        sys.argv = argv
        code = wx_hunt.main()
        if code == 0:
            sys.argv = ["map"]
            code = wx_map.main() or 0
    finally:
        sys.argv = old_argv
        sys.stdout = old_stdout
    return code or 0


def api_wx_hunt():
    # 测试版不带解密工具，直接返回提示，避免启动一个必然失败的后台任务
    if not any("wechat4-db-decrypt" in p for p in _tool_src_dirs()):
        return {"错误": "当前是测试版，不包含微信密钥抓取工具"}
    return start_job("微信密钥抓取(只读扫描)", lambda log_fn: _run_wx_hunt(log_fn))


# ---- QQ 密钥录入与校验 ----

def api_save_qqkey(key_text):
    """写入 QQ 密钥文件并用 HMAC 校验(只读活跃库第 1 页,不改动任何数据)。"""
    key_text = (key_text or "").strip().strip('"').strip("'")
    if not key_text:
        return {"成功": False, "错误": "密钥为空"}
    keyfile = cfg.qq_key_file()
    if not keyfile:
        return {"成功": False, "错误": "未配置 QQ 密钥文件路径(设置页 paths.qq_key_file)"}
    lib = cfg.qq_lib()
    # 1) 写入 key 文件（原文,不加换行 —— sync_qq.py 按原始字节读）
    os.makedirs(os.path.dirname(keyfile) or ".", exist_ok=True)
    with open(keyfile, "w", encoding="utf-8", newline="") as f:
        f.write(key_text)
    # 2) 校验
    if not lib or not os.path.exists(lib):
        return {"成功": True, "已写入": keyfile,
                "校验": "跳过(活跃库不存在: %s)" % (lib or "未配置")}
    try:
        helper = None
        for cand in (os.path.join(BASE, "tools", "qq-export-helper", "src"),
                     cfg.RES_DIR and os.path.join(cfg.RES_DIR, "tools", "qq-export-helper", "src")):
            if cand and os.path.isdir(cand):
                helper = cand
                break
        if not helper:
            return {"成功": True, "已写入": keyfile, "校验": "跳过(解密工具未打包)"}
        if helper not in sys.path:
            sys.path.insert(0, helper)
        from qqexport import sqlcipher_py as sp
        key = key_text.encode("utf-8")
        f = sp.get_file(lib, 1024, 4096)
        ok = bool(f.check(key, deep=False))
        return {"成功": True, "已写入": keyfile,
                "校验": "通过 ✓ key 有效" if ok else "失败 ✗ HMAC 不匹配(库或 key 不对应)",
                "校验通过": ok}
    except Exception as e:
        return {"成功": True, "已写入": keyfile, "校验": "校验异常: %s" % e}


def decrypt_sync_status():
    """密钥与索引页的状态汇总。"""
    return {
        "QQ密钥文件": cfg.qq_key_file() or "(未配置)",
        "QQ活跃库": cfg.qq_lib() or "(未配置)",
        "微信库目录": cfg.wechat_db_dir() or "(未配置)",
        "QQ最后同步": last_sync_time(cfg.qq_log()),
        "微信最后同步": last_sync_time(cfg.wechat_log()),
        "skill提示": ("QQ 密钥无法一键自动提取(QQ 反调试)。如密钥失效,请让 AI 会话"
                      "按内置 skill「qq-key-recovery」执行恢复,或在本页手动录入新 key。"),
    }


# ---------------------------------------------------------------- HTTP

def _decode_body(raw):
    """兼容 UTF-8 / GBK 请求体（Windows 控制台 curl 常发 GBK）。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", "replace")


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        path = parsed.path
        if path.startswith("/web/"):
            self._serve_static(path)
            return
        if path == "/" or path == "/index.html":
            self._serve_page()
        elif path == "/api/conversations":
            platform = q.get("platform", ["QQ"])[0]
            self._send({"会话": list_conversations(platform)})
        elif path == "/api/contacts":
            self._send(api_contacts(q.get("q", [""])[0]))
        elif path == "/api/group_members":
            self._send(api_group_members(q.get("gnum", [""])[0]))
        elif path == "/api/sync/status":
            self._send(api_sync_status())
        elif path == "/api/favorites":
            self._send(extras.api_favorites())
        elif path == "/api/stats":
            self._send(extras.api_stats_insights(q.get("platform", ["QQ"])[0]))
        elif path == "/api/log":
            try:
                lines = int(q.get("lines", ["300"])[0])
            except ValueError:
                lines = 300
            self._send(extras.api_log_tail(q.get("which", ["qq"])[0], lines))
        elif path == "/api/health":
            self._send(extras.api_health_check())
        elif path == "/api/autostart":
            self._send(extras.api_autostart())
        elif path == "/api/trash":
            self._send(extras.api_trash_list())
        elif path == "/api/search":
            platform = q.get("platform", ["QQ"])[0]
            query = q.get("q", [""])[0].strip()
            if not query and not (q.get("conv", [""])[0] or q.get("sender", [""])[0]
                                  or q.get("from", [""])[0] or q.get("to", [""])[0]):
                self._send({"结果": [], "总数": 0})
                return
            try:
                offset = max(0, int(q.get("offset", ["0"])[0]))
                limit = min(500, max(1, int(q.get("limit", ["50"])[0])))
            except ValueError:
                offset, limit = 0, 50
            self._send(api_search(
                platform, query,
                q.get("regex", ["0"])[0] == "1",
                q.get("conv", [""])[0],
                q.get("from", [""])[0], q.get("to", [""])[0],
                q.get("sender", [""])[0].strip(),
                q.get("all", ["0"])[0] == "1",
                offset, limit))
        elif path == "/api/convs":
            self._send(api_convs(q.get("platform", ["QQ"])[0]))
        elif path == "/api/calendar":
            self._send(api_calendar(q.get("platform", ["QQ"])[0],
                                    q.get("year", ["0"])[0],
                                    q.get("month", ["0"])[0]))
        elif path == "/api/senders":
            self._send(api_senders(q.get("platform", ["QQ"])[0],
                                   q.get("conv", [""])[0]))
        elif path == "/api/export":
            self._serve_export(q)
        elif path == "/api/conv":
            try:
                aid = int(q.get("id", ["0"])[0] or 0)
            except ValueError:
                aid = 0
            try:
                nav = int(q.get("nav", ["0"])[0] or 0)
            except ValueError:
                nav = 0
            self._send(api_conv(q.get("platform", ["QQ"])[0],
                                q.get("conv", [""])[0],
                                q.get("date", [""])[0], aid, nav))
        elif path == "/api/index/status":
            self._send(api_index_status())
        elif path == "/api/keystatus":
            self._send(key_status())
        elif path == "/api/testkey":
            self._send(test_qq_key())
        elif path == "/api/config":
            self._send(api_get_config())
        elif path == "/api/config/test":
            self._send(api_test_config())
        elif path == "/api/skill":
            self._send(api_skill_text(q.get("name", [""])[0]))
        elif path == "/api/setup/state":
            self._send(api_setup_state())
        elif path == "/api/preflight":
            self._send(api_preflight())
        elif path == "/api/decrystatus":
            self._send(decrypt_sync_status())
        elif path == "/api/job":
            self._send(api_job(q.get("id", ["0"])[0]))
        else:
            self._send({"错误": "未知路径"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/chat":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(_decode_body(self.rfile.read(length)))
            text = body.get("text", "")
            history = body.get("history", [])
            use_context = bool(body.get("use_context", False))
            context_query = body.get("context_query", "")
            platform = body.get("platform", "QQ")
            prov = body.get("provider", None)
            prov_idx = None
            try:
                prov_idx = int(prov)
            except (TypeError, ValueError):
                prov_idx = None
            result = do_chat(text, history, use_context, context_query, platform, prov_idx)
            self._send(result)
        elif parsed.path == "/api/provider/test":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(_decode_body(self.rfile.read(length)))
            self._send(api_test_provider(body))
        elif parsed.path == "/api/config":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(_decode_body(self.rfile.read(length)))
            self._send(api_save_config(body))
        elif parsed.path == "/api/setup/init":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(_decode_body(self.rfile.read(length))) if length else {}
            self._send(api_setup_init(body.get("data_root", "")))
        elif parsed.path == "/api/setup/paths":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(_decode_body(self.rfile.read(length))) if length else {}
            self._send(api_setup_paths(body))
        elif parsed.path == "/api/setup/done":
            self._send(api_setup_done())
        elif parsed.path == "/api/sync":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(_decode_body(self.rfile.read(length)))
            self._send(api_start_sync(body.get("target", ""), bool(body.get("dry", False))))
        elif parsed.path == "/api/conv/delete":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(_decode_body(self.rfile.read(length))) if length else {}
            self._send(api_conv_delete(
                body.get("platform", "QQ"), body.get("conv", ""),
                bool(body.get("protect", True)), bool(body.get("dry", False))))
        elif parsed.path == "/api/index/build":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(_decode_body(self.rfile.read(length))) if length else {}
            self._send(api_index_build(bool(body.get("force", False))))
        elif parsed.path == "/api/wxkey":
            self._send(api_wx_hunt())
        elif parsed.path == "/api/qqkey":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(_decode_body(self.rfile.read(length)))
            self._send(api_save_qqkey(body.get("key", "")))
        elif parsed.path == "/api/favorites/toggle":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(_decode_body(self.rfile.read(length))) if length else {}
            self._send(extras.api_favorite_toggle(body))
        elif parsed.path == "/api/favorites/remove":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(_decode_body(self.rfile.read(length))) if length else {}
            self._send(extras.api_favorite_remove(body.get("key", "")))
        elif parsed.path == "/api/autostart":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(_decode_body(self.rfile.read(length))) if length else {}
            self._send(extras.api_autostart(bool(body.get("enable"))))
        elif parsed.path == "/api/trash/restore":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(_decode_body(self.rfile.read(length))) if length else {}
            self._send(extras.api_trash_restore(
                body.get("batch", ""), body.get("platform", ""),
                body.get("sub", ""), body.get("dir", "")))
        elif parsed.path == "/api/import/scan":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(_decode_body(self.rfile.read(length))) if length else {}
            self._send(extras.api_import_scan(body.get("path", "")))
        elif parsed.path == "/api/import/copy":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(_decode_body(self.rfile.read(length))) if length else {}
            self._send(extras.api_import_copy(body.get("path", ""), body.get("platform", "QQ")))
        else:
            self._send({"错误": "未知路径"}, 404)

    def _serve_export(self, q):
        platform = q.get("platform", ["QQ"])[0]
        conv = q.get("conv", [""])[0]
        fmt = q.get("format", ["md"])[0]
        if fmt not in ("md", "txt", "html"):
            fmt = "md"
        if not conv:
            self._send({"错误": "缺少会话参数"}, 400)
            return
        try:
            name, data = export_bytes(platform, conv, fmt,
                                      q.get("from", [""])[0], q.get("to", [""])[0])
        except Exception as e:
            self._send({"错误": "导出失败: %s" % e}, 500)
            return
        safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip(" .")[:60] or conv
        fname = "%s_%s.%s" % (safe, platform, fmt)
        ctype = {"txt": "text/plain; charset=utf-8",
                 "html": "text/html; charset=utf-8"}.get(
            fmt, "text/markdown; charset=utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition",
                         "attachment; filename*=UTF-8''%s" % quote(fname))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, path):
        """静态资源（web/ 下的 CSS/JS/图片）；打包后在 _MEIPASS，开发时在脚本目录。"""
        rel = path[len("/web/"):].replace("\\", "/")
        if not rel or ".." in rel.split("/"):
            self._send({"错误": "not found"}, 404)
            return
        mimes = {".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".svg": "image/svg+xml", ".png": "image/png",
                 ".json": "application/json; charset=utf-8"}
        for base in (cfg.RES_DIR, BASE):
            p = os.path.join(base, "web", *rel.split("/"))
            if not os.path.isfile(p):
                continue
            try:
                with open(p, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            ext = os.path.splitext(p)[1].lower()
            self.send_response(200)
            self.send_header("Content-Type", mimes.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        self._send({"错误": "not found"}, 404)

    def _serve_page(self):
        # 打包后 HTML 在 _MEIPASS；开发时在脚本目录。两处都找一遍。
        candidates = [os.path.join(cfg.RES_DIR, "chatvault.html"),
                      os.path.join(BASE, "chatvault.html")]
        data = None
        for page_path in candidates:
            try:
                with open(page_path, encoding="utf-8") as f:
                    data = f.read().encode("utf-8")
                break
            except Exception:
                continue
        if data is None:
            data = "<h1>页面缺失: chatvault.html</h1>".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        # 关键：禁止 WebView 缓存 HTML，否则升级后窗口仍在跑旧脚本
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def make_server(port=8765):
    """创建（但不启动）本地服务，供脚本模式与窗口模式复用。"""
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    try:
        ensure_index_async()
    except Exception:
        pass
    try:
        start_sync_scheduler()
    except Exception:
        pass
    return srv


def main():
    ap = argparse.ArgumentParser(description="个人聊天数据本地工作台")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    print("=" * 60)
    print("个人聊天数据本地工作台")
    print("地址: http://127.0.0.1:%d" % args.port)
    print("安全: 仅本机可访问 · 搜索全本地 · 对话强制脱敏")
    if not cfg.is_configured():
        print("[提示] " + cfg.setup_hint())
    print("=" * 60)
    make_server(args.port).serve_forever()


if __name__ == "__main__":
    main()
