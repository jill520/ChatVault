#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
local_config.py - 本地配置加载器（公共模块，不含任何真实密钥或路径）

配置优先级：
  环境变量 > .local/config.json（本机真实值，git 忽略）> config.example.json（空模板）

设计要点：
  - 纯标准库、零网络、零 AI
  - 只提供取值接口，不打印、不回显任何密钥
  - 本文件可安全上传 GitHub；真实值永远留在 .local/
"""
import os
import json
import sys

# 打包环境感知：
#   RES_DIR = 资源目录（打包后是临时解压目录，开发时是脚本目录）
#   APP_DIR = 配置目录（打包后是 exe 所在目录，保证配置持久且不随解压目录消失）
if getattr(sys, "frozen", False):
    RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    RES_DIR = os.path.dirname(os.path.abspath(__file__))
    APP_DIR = RES_DIR

BASE = APP_DIR  # 相对路径以配置目录为基准
LOCAL = os.path.join(APP_DIR, ".local")
LOCAL_FILE = os.path.join(LOCAL, "config.json")
EXAMPLE_FILE = os.path.join(RES_DIR, "config.example.json")

# 环境变量覆盖（便于 CI / 多机部署，不必落盘密钥）
ENV_MAP = {
    ("glm", "api_key"): "CHATVAULT_GLM_KEY",
    ("paths", "qq_archive"): "CHATVAULT_QQ_ARCHIVE",
    ("paths", "wechat_archive"): "CHATVAULT_WX_ARCHIVE",
    ("paths", "qq_summary_out"): "CHATVAULT_QQ_OUT",
    ("paths", "wechat_summary_out"): "CHATVAULT_WX_OUT",
    ("paths", "qq_key_file"): "CHATVAULT_QQ_KEYFILE",
    ("paths", "qq_lib"): "CHATVAULT_QQ_LIB",
    ("paths", "qq_log"): "CHATVAULT_QQ_LOG",
    ("paths", "wechat_log"): "CHATVAULT_WX_LOG",
    ("paths", "qq_incr"): "CHATVAULT_QQ_INCR",
    ("paths", "wechat_incr"): "CHATVAULT_WX_INCR",
    ("paths", "wechat_db_dir"): "CHATVAULT_WX_DB",
    ("paths", "index_db"): "CHATVAULT_INDEX_DB",
    ("paths", "trash"): "CHATVAULT_TRASH",
    ("paths", "all_keys"): "CHATVAULT_WX_KEYS",
    ("paths", "data_root"): "CHATVAULT_DATA_ROOT",
    ("account", "qq"): "CHATVAULT_QQ_UIN",
}


def _merge(base, override):
    """递归合并：override 覆盖 base，未提供的键保留 base。"""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load():
    cfg = _merge(_read(EXAMPLE_FILE), _read(LOCAL_FILE))
    for (section, key), env in ENV_MAP.items():
        val = os.environ.get(env)
        if val is not None and val != "":
            cfg.setdefault(section, {})[key] = val
    return cfg


CFG = _load()


def _abs(p):
    """相对路径按程序目录解析。"""
    if not p:
        return ""
    return p if os.path.isabs(p) else os.path.join(BASE, p)


# ---- 数据根（v1.2.5）：新用户只填一个目录，其余路径自动派生 ----
DEFAULT_ROOT = os.path.join(os.path.expanduser("~"), "Documents", "ChatVault")
# 老用户机器上已存在的旧默认目录：优先沿用，避免升级后路径突变
LEGACY_DIRS = {
    "qq_archive": "",
    "wechat_archive": "",
    "qq_incr": "",
    "wechat_incr": "",
}
LEGACY_FILES = {
    "qq_key_file": "qq_pc_key.txt",
    "qq_log": "qq_sync.log",
    "wechat_log": "wechat_sync.log",
}


def data_root():
    """所有数据的总目录。留空 = 文档\\ChatVault（任何账号可写）。"""
    p = _abs((CFG.get("paths", {}) or {}).get("data_root") or "")
    return p or DEFAULT_ROOT


def _root_configured():
    """用户是否主动选过数据根（向导/设置页保存过）。"""
    return bool(_abs((CFG.get("paths", {}) or {}).get("data_root") or ""))


def _derived(key, rel):
    """显式字段 > 显式数据根 > 老默认目录（存在时）> 默认数据根。"""
    p = _abs((CFG.get("paths", {}) or {}).get(key) or "")
    if p:
        return p
    if _root_configured():
        return os.path.join(data_root(), rel)
    legacy = LEGACY_DIRS.get(key) or ""
    if legacy and os.path.isdir(legacy):
        return legacy
    return os.path.join(data_root(), rel)


def _derived_file(key, rel):
    p = _abs((CFG.get("paths", {}) or {}).get(key) or "")
    if p:
        return p
    old = LEGACY_FILES.get(key) or ""
    if old and os.path.exists(os.path.join(BASE, old)):
        return os.path.join(BASE, old)
    return os.path.join(data_root(), rel)


def ensure_layout():
    """按数据根创建标准子目录（幂等），返回新建了哪些。"""
    root = data_root()
    subs = [r"归档\QQ", r"归档\微信", r"增量\QQ", r"增量\微信", "回收站",
            r"导出\QQ", r"导出\微信", "索引", "密钥", "日志"]
    made = []
    os.makedirs(root, exist_ok=True)
    for s in subs:
        d = os.path.join(root, s)
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
            made.append(d)
    return {"数据根": root, "新建": made}


# ---- GLM ----
def glm_key():
    return str(CFG.get("glm", {}).get("api_key") or "")


def glm_api():
    return str(CFG.get("glm", {}).get("api") or
               "https://open.bigmodel.cn/api/paas/v4/chat/completions")


def glm_model():
    return str(CFG.get("glm", {}).get("model") or "glm-4-flash")


# ---- 模型服务（多 Provider,OpenAI 兼容协议）----
# providers: [{"name","api","key","model"}],本地模型(Ollama/LM Studio) key 留空
def providers():
    """返回模型服务列表;旧的 glm 字段自动迁移为第一个服务。"""
    ps = CFG.get("providers")
    if isinstance(ps, list) and ps:
        return [p for p in ps if isinstance(p, dict)]
    # 迁移: 老配置只有 glm 段
    return [{
        "name": "智谱GLM",
        "api": glm_api(),
        "key": glm_key(),
        "model": glm_model(),
    }]


def active_provider():
    """当前使用的服务下标(越界自动回到 0)。"""
    ps = providers()
    i = CFG.get("active_provider", 0)
    try:
        i = int(i)
    except Exception:
        i = 0
    return i if 0 <= i < len(ps) else 0


def provider_at(i):
    ps = providers()
    try:
        i = int(i)
    except Exception:
        i = 0
    if 0 <= i < len(ps):
        return ps[i]
    return ps[0] if ps else {"name": "", "api": "", "key": "", "model": ""}


def is_local_api(api):
    """本地服务(Ollama/LM Studio 等)不需要密钥。"""
    a = (api or "").lower()
    return "127.0.0.1" in a or "localhost" in a or "0.0.0.0" in a


# ---- 对话行为 ----
def chat_system_prompt():
    """用户自定义对话提示词(留空用内置默认)。"""
    return str(CFG.get("chat", {}).get("system_prompt") or "")


# ---- 归档 / 输出路径（留空时按数据根派生） ----
def qq_archive():
    return _derived("qq_archive", r"归档\QQ")


def wechat_archive():
    return _derived("wechat_archive", r"归档\微信")


def qq_summary_out():
    return _derived("qq_summary_out", r"导出\QQ")


def wechat_summary_out():
    return _derived("wechat_summary_out", r"导出\微信")


def roots():
    """chatvault 用的平台 → 归档根 映射。"""
    return {"QQ": qq_archive(), "微信": wechat_archive()}


def out_dirs():
    """summarize_chat 用的平台 → 输出根 映射。"""
    return {"QQ": qq_summary_out(), "WX": wechat_summary_out()}


# ---- 密钥 / 库 / 日志 ----
def qq_key_file():
    return _derived_file("qq_key_file", r"密钥\qq_pc_key.txt")


def qq_lib():
    return _abs(CFG.get("paths", {}).get("qq_lib", ""))


def qq_log():
    return _derived_file("qq_log", r"日志\qq_sync.log")


def wechat_log():
    return _derived_file("wechat_log", r"日志\wechat_sync.log")


def qq_incr():
    return _derived("qq_incr", r"增量\QQ")


def wechat_incr():
    return _derived("wechat_incr", r"增量\微信")


def wechat_db_dir():
    return _abs(CFG.get("paths", {}).get("wechat_db_dir", ""))


def index_db():
    """检索索引库路径。

    显式配置 > 显式数据根（向导建的新布局）> 老用户已有的
    <程序目录>/.local/index.db（避免升级后丢 1.1GB 索引）> 数据根\\索引\\index.db。
    """
    p = (CFG.get("paths", {}) or {}).get("index_db") or ""
    if p:
        return _abs(p)
    if _root_configured():
        return os.path.join(data_root(), "索引", "index.db")
    old = os.path.join(LOCAL, "index.db")
    if os.path.exists(old):
        return old
    return os.path.join(data_root(), "索引", "index.db")


def trash_dir():
    """删除会话时的回收站目录（不真删，整体移到这里，可搬回）。

    优先级：paths.trash > 显式数据根 > 旧默认 F:\\垃圾中转站（存在时，兼容老用户）
            > 数据根\\回收站。
    """
    p = _abs((CFG.get("paths", {}) or {}).get("trash") or "")
    if p:
        return p
    if _root_configured():
        return os.path.join(data_root(), "回收站")
    legacy = r"F:\垃圾中转站"
    if os.path.isdir(legacy):
        return legacy
    return os.path.join(data_root(), "回收站")


def qq_uin():
    return str(CFG.get("account", {}).get("qq", ""))


# ---- 微信密钥与解密工具运行时（exe 内 _MEIPASS 不可写，必须落到数据目录） ----
def all_keys_file():
    """微信 all_keys.json 的持久位置。

    显式 paths.all_keys > 显式数据根\\密钥 > 老位置 tools/wechat4-db-decrypt（存在时）
    > 默认数据根\\密钥。
    """
    p = _abs((CFG.get("paths", {}) or {}).get("all_keys") or "")
    if p:
        return p
    new = os.path.join(data_root(), "密钥", "all_keys.json")
    runtime = os.path.join(LOCAL, "wx_tool", "all_keys.json")
    old = os.path.join(BASE, "tools", "wechat4-db-decrypt", "all_keys.json")
    for c in (new, runtime, old):
        if os.path.exists(c):
            return c
    return new


def wx_runtime_dir():
    """微信解密工具的可写运行时目录（放 config.json / hunted_keys / decrypted）。

    老用户放程序目录 .local\\wx_tool，不覆盖工具自带的 config.json；
    新用户按数据根放 <数据根>\\密钥\\wx_tool。
    """
    d = (os.path.join(data_root(), "密钥", "wx_tool") if _root_configured()
         else os.path.join(LOCAL, "wx_tool"))
    os.makedirs(d, exist_ok=True)
    return d


def ensure_wx_tool_config():
    """生成可写的微信解密配置，避免往 _MEIPASS 写（退出即丢）。"""
    d = wx_runtime_dir()
    cfgp = os.path.join(d, "config.json")
    db_dir = wechat_db_dir() or ""
    if os.path.exists(cfgp):
        try:
            with open(cfgp, encoding="utf-8") as f:
                old = json.load(f)
            if not db_dir and old.get("db_dir"):
                db_dir = old["db_dir"]
        except Exception:
            pass
    data = {
        "db_dir": db_dir,
        "keys_file": all_keys_file(),
        "decrypted_dir": os.path.join(d, "decrypted"),
        "hunted_file": os.path.join(d, "hunted_keys.txt"),
        "snapshot_dir": os.path.join(d, ".runtime", "win-snapshot"),
        "platform": "windows",
    }
    tmp = cfgp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp, cfgp)
    return {"配置": cfgp, "密钥": all_keys_file(), "目录": d}


def _has_md(d, max_depth=3):
    """目录里有没有 md 归档（限深，避免在超大目录上遍历过久）。"""
    if not d or not os.path.isdir(d):
        return False
    base = os.path.abspath(d)
    for root, dirs, files in os.walk(base):
        if root.count(os.sep) - base.count(os.sep) >= max_depth:
            dirs[:] = []
        if any(f.endswith(".md") for f in files):
            return True
    return False


def is_configured():
    """有可读的 md 归档/增量才算就绪。

    注意：不能拿"索引库文件存在"当依据——首次启动时 ensure_index_async 会先建一个
    空索引库，否则新用户会被误判为已配置，向导永远不弹（v1.2.5 修正）。
    """
    return any(_has_md(d) for d in (qq_archive(), wechat_archive(), qq_incr(), wechat_incr()))


def paths_ready():
    """配置门禁：显式配置的数据根/归档目录存在，或已有归档时才放行。

    不再用 setup.json 的 done 标记——路径被删/盘符变化时，下次启动应重新引导，
    否则用户会进入一个所有路径都失效的界面。
    """
    try:
        if is_configured():
            return True
        if _root_configured() and os.path.isdir(data_root()):
            return True
        paths = CFG.get("paths", {}) or {}
        for key, fn in (("qq_archive", qq_archive), ("wechat_archive", wechat_archive),
                        ("qq_incr", qq_incr), ("wechat_incr", wechat_incr)):
            if str(paths.get(key) or "").strip():
                try:
                    if os.path.isdir(fn()):
                        return True
                except Exception:
                    pass
    except Exception:
        pass
    return False


SETUP_FILE = os.path.join(LOCAL, "setup.json")


def setup_done():
    """首次向导是否已完成（老用户已配置时自动视为完成，见 chatvault 的 setup/state）。"""
    try:
        with open(SETUP_FILE, encoding="utf-8") as f:
            return bool(json.load(f).get("done"))
    except Exception:
        return False


def mark_setup_done(extra=None):
    os.makedirs(LOCAL, exist_ok=True)
    data = {"done": True}
    if extra:
        data.update(extra)
    tmp = SETUP_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, SETUP_FILE)
    return True


def setup_hint():
    return ("首次使用请按向导选择数据根；也可以稍后在「设置」页手动填路径，"
            "或在「密钥与索引」页导入本机 QQ/微信。")


# ---- 设置的读取与保存（供软件内设置界面使用） ----

def get_all():
    """返回当前合并后的配置（含真实值，仅本地回环接口使用）。"""
    # 深拷贝，避免调用方改动污染全局 CFG
    import copy
    return copy.deepcopy(CFG)


def sync_cfg():
    """自动同步配置（软件运行期间生效），缺失字段给安全默认值。"""
    s = CFG.get("sync") or {}
    if not isinstance(s, dict):
        s = {}
    targets = s.get("targets")
    if not isinstance(targets, list):
        targets = ["qq", "wechat"]
    targets = [t for t in targets if t in ("qq", "wechat")]
    if not targets:
        targets = ["qq"]
    try:
        interval = int(s.get("interval_minutes", 60))
    except (TypeError, ValueError):
        interval = 60
    interval = max(5, min(1440, interval))
    daily = str(s.get("daily_time") or "00:30").strip()
    try:
        hh, mm = daily.split(":")
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            raise ValueError
        daily = "%02d:%02d" % (int(hh), int(mm))
    except (ValueError, AttributeError):
        daily = "00:30"
    return {
        "enabled": bool(s.get("enabled", True)),
        "mode": "daily" if str(s.get("mode") or "interval") == "daily" else "interval",
        "interval_minutes": interval,
        "daily_time": daily,
        "targets": targets,
        "on_startup": bool(s.get("on_startup", True)),
        "rebuild_index": bool(s.get("rebuild_index", True)),
    }


def save_config(new_cfg):
    """保存配置到 .local/config.json 并热重载，立即生效。

    只写程序自己的配置文件，不触碰任何归档、密钥库或原始数据文件。
    """
    global CFG
    if not isinstance(new_cfg, dict):
        raise ValueError("配置格式错误")
    # 去掉说明字段以外的未知顶层键，保持结构干净
    clean = {
        "glm": new_cfg.get("glm", {}),
        "providers": new_cfg.get("providers", []),
        "active_provider": new_cfg.get("active_provider", 0),
        "chat": new_cfg.get("chat", {}),
        "paths": new_cfg.get("paths", {}),
        "account": new_cfg.get("account", {}),
        "sync": new_cfg.get("sync", {}),
    }
    if not isinstance(clean["providers"], list):
        clean["providers"] = []
    os.makedirs(LOCAL, exist_ok=True)
    tmp = LOCAL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LOCAL_FILE)
    CFG = _load()  # 热重载：所有 getter 立即读到新值
    return True


def config_path():
    """配置文件实际位置（展示用）。"""
    return LOCAL_FILE
