# -*- coding: utf-8 -*-
r"""群会话键别名：同一群曾以「群号」和「群内部 ID」两个键各落一个目录。

2026-11-18 数据质量审计查出 10 组双键群（详见
`docs/data-audit.md`）。
归档里已合并为一份，这里登记"别名 → 主键"，供同步/补录脚本归一，
防止再长出重复目录。

用法:
    from group_alias import normalize
    key = normalize("3808736864")   # -> "318736864"
"""
import json
import os

ALIAS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "group_alias.json")

_cache = None


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


def normalize(key):
    """把群会话键归一成归档目录所用的主键；未登记的原样返回。"""
    k = str(key or "").strip()
    return load().get(k, k)
