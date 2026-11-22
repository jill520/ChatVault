# -*- coding: utf-8 -*-
"""会话退役名单：被移除的会话不再由同步/导入脚本重建。

excluded.json 结构：
    {"群聊": ["1058875455", "某某群(123456)"], "私聊": ["QQ123456789"]}

用途：用户要求删掉某个群/某个人的聊天记录后，光删目录不够——sync_qq 下轮
同步到该会话的新消息会重新建目录，import_backup 重跑也会补回来。登记到
这里即可让写入端跳过。

文件位置（2026-11-18 修正）：
  旧版放在模块目录（dirname(__file__)），打包成 onefile 后指向 _MEIPASS
  临时解压目录，exe 每次启动都读不到名单，软件内登记也会随退出丢失。
  现在与索引库同目录（配置项 paths.index_db 的所在目录），源码与 exe
  共用同一份；旧文件在首次读取时自动合并过去，原文件保留不删。
"""
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
LEGACY = os.path.join(BASE, "excluded.json")


def _default_path():
    try:
        import local_config as _cfg
        d = os.path.dirname(_cfg.index_db())
        if d:
            return os.path.join(d, "excluded.json")
    except Exception:
        pass
    return LEGACY


PATH = _default_path()
_cache = None


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(d):
    global _cache
    try:
        os.makedirs(os.path.dirname(PATH) or ".", exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, PATH)
        _cache = d
    except Exception:
        _cache = d


def load():
    global _cache
    if _cache is not None:
        return _cache
    if os.path.exists(PATH):
        _cache = _read(PATH)
    elif os.path.exists(LEGACY) and os.path.abspath(LEGACY) != os.path.abspath(PATH):
        _cache = _read(LEGACY)
        if _cache:
            _write(_cache)
    else:
        _cache = {}
    return _cache


def keys(sub):
    return set(load().get(sub, []))


import re

_NUM = re.compile(r"\((\d{5,})\)$")
_PURE = re.compile(r"^(\d{5,})$")
_QQ = re.compile(r"^QQ\s*(\d{5,12})", re.I)


def _ids(s):
    """把各种写法归一：群名(123456) / 群名(123@chatroom) / QQ123456 / 纯数字。

    括号里的内容本身也作为候选，这样"群名(群ID)"与裸"群ID"能互认
    （微信群的目录名带 (xxx@chatroom)，同步消息里往往是裸 ID）。
    """
    s = str(s or "").strip()
    out = {s}
    out |= set(re.findall(r"\(([^()]+)\)", s))
    for c in list(out):
        for rx in (_NUM, _PURE, _QQ):
            m = rx.search(c)
            if m:
                out.add(m.group(1))
    return out


def is_excluded(sub, conv):
    """conv 传目录名或裸群号/QQ号都能命中（两侧都按号归一后比较）。"""
    ks = keys(sub)
    if not ks:
        return False
    cand = _ids(conv)
    known = set()
    for k in ks:
        known |= _ids(k)
    return bool(cand & known)


def add(sub, conv):
    d = load()
    lst = d.setdefault(sub, [])
    if conv not in lst:
        lst.append(conv)
        _write(d)
    return len(lst)


def remove(sub, conv):
    d = load()
    lst = d.get(sub, [])
    if conv in lst:
        lst.remove(conv)
        _write(d)
        return True
    return False


def all_items():
    """给设置页/接口用的只读快照。"""
    d = load()
    return {sub: list(d.get(sub, [])) for sub in ("群聊", "私聊")}

def reset():
    """索引库路径变更后重算名单位置，并丢弃已缓存内容。"""
    global PATH, _cache
    PATH = _default_path()
    _cache = None