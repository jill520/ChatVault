# -*- coding: utf-8 -*-
r"""私聊会话键别名：把 uid / 临时会话 / 「昵称(QQ)」归一到归档目录名。

2026-11-18 交接任务 B。背景：增量侧出现 u_xxx 目录，与归档里的
QQ<号>[_后缀] 目录是同一人，下次同步会再长出重复会话
（群聊侧当年就是这么虚增 66.5 万条的）。

接口与 group_alias.py 同构：
    from user_alias import normalize
    key = normalize("u_ZJbRPWVDrFDlfi9La1X6PQ")
    # -> "QQ2087427311_9La1X6PQ"

规则（依次）：
  1. user_alias.json 精确命中 -> 直接返回登记值
  2. u_xxx 且 uid_map.json 有 QQ 号 -> 返回归档中该 QQ 的目录名
  3. 「昵称(QQ号)」/「QQ号[_后缀]」 -> 若归档已有该 QQ 目录则归一，否则原样
  4. 其余原样返回
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    import local_config as _lcfg
    APPDIR = _lcfg.APP_DIR          # 打包后是 exe 目录，源码模式是脚本目录
    ARCH_PRIV = os.path.join(_lcfg.qq_archive(), "私聊") if _lcfg.qq_archive() else ""
except Exception:
    APPDIR = HERE
    ARCH_PRIV = ""
ALIAS_FILE = os.path.join(HERE, "user_alias.json")
UID_MAP_FILE = os.path.join(APPDIR, "uid_map.json")

_cache = None
_uidmap = None
_dirmap = None


def load():
    global _cache
    if _cache is None:
        data = {}
        for p in (ALIAS_FILE, ALIAS_FILE.replace(".json", ".example.json")):
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                if data:
                    break
            except Exception:
                continue
        _cache = {str(k): str(v) for k, v in data.items()}
    return _cache


def _uid_map():
    global _uidmap
    if _uidmap is None:
        try:
            with open(UID_MAP_FILE, encoding="utf-8") as f:
                _uidmap = json.load(f)
        except Exception:
            _uidmap = {}
    return _uidmap


def _dir_map():
    """QQ号 -> 归档私聊目录名。QQ<号> 形式优先，排除 u_/临时会话。"""
    global _dirmap
    if _dirmap is not None:
        return _dirmap
    d = {}
    try:
        for name in sorted(os.listdir(ARCH_PRIV)):
            if not os.path.isdir(os.path.join(ARCH_PRIV, name)):
                continue
            if name.startswith("u_") or "临时会话" in name:
                continue
            m = (re.match(r"^QQ(\d{5,})(?:_|$)", name)
                 or re.search(r"\((\d{5,})\)(?:_[A-Za-z0-9\-]{8})?$", name))
            if not m:
                continue
            qq = m.group(1)
            old = d.get(qq)
            if old is None or (name.startswith("QQ" + qq) and not old.startswith("QQ" + qq)):
                d[qq] = name
    except Exception:
        pass
    _dirmap = d
    return d


def _qq_of(key):
    if key.startswith("u_"):
        info = _uid_map().get(key)
        if isinstance(info, dict):
            return str(info.get("qq") or "").strip()
        return str(info or "").strip()
    m = (re.search(r"\((\d{5,})\)", key)
         or re.match(r"^QQ(\d{5,})(?:_|$)", key))
    return m.group(1) if m else ""


def normalize(key):
    """把私聊会话键归一成归档目录名；无把握的原样返回。"""
    k = str(key or "").strip()
    if not k:
        return k
    try:
        hit = load().get(k)
        if hit:
            return hit
        qq = _qq_of(k)
        if qq:
            return _dir_map().get(qq, "QQ" + qq)
    except Exception:
        pass
    return k