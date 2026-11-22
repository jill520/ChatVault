#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chatvault_app.py - 独立窗口版启动器（PyInstaller 入口）

职责：
  1. 后台线程启动本地 HTTP 服务（复用 chatvault.py，随机空闲端口，避免冲突）
  2. 主线程用 pywebview 打开一个无地址栏的独立软件窗口
  3. 配置持久化到 exe 所在目录的 .local/（由 local_config 处理）
  4. 关窗时干净退出服务

安全红线（与 chatvault 一致）:
  - 服务只监听 127.0.0.1
  - 搜索全本地；对话强制脱敏
  - 只读归档，不删任何文件
"""
import os
import sys
import json
import ctypes
import threading
import socket
import webview

import chatvault
import local_config as cfg

APP_TITLE = "聊天档案工作台"
WIN_W, WIN_H = 1180, 800
GEOM_FILE = os.path.join(cfg.LOCAL, "window.json")


def load_geom():
    """读取上次窗口尺寸/位置；缺失或损坏则用默认值。"""
    try:
        with open(GEOM_FILE, encoding="utf-8") as f:
            g = json.load(f)
        return g, max(900, int(g.get("w") or WIN_W)), max(600, int(g.get("h") or WIN_H))
    except Exception:
        return {}, WIN_W, WIN_H


def save_geom(window):
    """关窗时记住尺寸和位置，下次原样恢复。"""
    try:
        g = {"w": int(window.width), "h": int(window.height)}
        try:
            g["x"] = int(window.x)
            g["y"] = int(window.y)
        except Exception:
            pass
        os.makedirs(cfg.LOCAL, exist_ok=True)
        with open(GEOM_FILE, "w", encoding="utf-8") as f:
            json.dump(g, f)
    except Exception:
        pass


def acquire_single_instance():
    """Windows 命名互斥：避免同时开多份写同一个索引库。

    返回句柄（必须保留引用直到退出）；已有一份在运行时返回 None。
    """
    if os.name != "nt":
        return True
    try:
        k32 = ctypes.windll.kernel32
        handle = k32.CreateMutexW(None, False, "Global\\ChatVaultWorkbench")
        if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            ctypes.windll.user32.MessageBoxW(
                0, "聊天档案工作台已经在运行。\n请切换到已打开的窗口。",
                APP_TITLE, 0x40)
            return None
        return handle
    except Exception:
        return True


def pick_port():
    """让系统分配一个空闲端口，避免与已运行的服务/其它程序冲突。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def ensure_config():
    """首次运行时，在 exe 目录生成空配置模板，并告知用户位置。"""
    if os.path.exists(cfg.LOCAL_FILE):
        return True
    try:
        os.makedirs(cfg.LOCAL, exist_ok=True)
        with open(cfg.EXAMPLE_FILE, encoding="utf-8") as src:
            content = src.read()
        with open(cfg.LOCAL_FILE, "w", encoding="utf-8") as dst:
            dst.write(content)
        return False
    except Exception:
        return False


def _diag_probe(window):
    """CHATVAULT_DIAG=1 时采样页面布局，写 .local/diag.log（排查空白页用）。"""
    import time as _t
    js = r"""
    (function(){
      try{ if(typeof switchTab==='function') switchTab('convs'); }catch(e){}
      function q(s){var e=document.querySelector(s); if(!e) return null;
        var cs=getComputedStyle(e), r=e.getBoundingClientRect();
        return {wh:[e.offsetWidth,e.offsetHeight],xy:[Math.round(r.x),Math.round(r.y)],
          d:cs.display,vis:cs.visibility,op:cs.opacity,z:cs.zIndex,pos:cs.position,
          bg:cs.backgroundColor,fg:cs.color,ov:cs.overflow};}
      var sq=document.querySelector('#p-convs .sq'), hit=null;
      if(sq){var sr=sq.getBoundingClientRect();
        var h=document.elementFromPoint(sr.left+20, sr.top+20); hit=h?(h.tagName+'.'+h.className):null;}
      var list=document.querySelector('#v-list');
      return JSON.stringify({iw:innerWidth,ih:innerHeight,dpr:devicePixelRatio,
        body:q('body'),main:q('#main'),content:q('.content'),wrap:q('.wrap'),
        pc:q('#p-convs'),sq:q('#p-convs .sq'),sb:q('#p-convs .sq-body'),
        tab:document.body.dataset.tab,theme:document.documentElement.dataset.theme,
        hit:hit,listLen:list?list.innerHTML.length:-1,
        rows:(typeof CV!=='undefined'&&CV.rows)?CV.rows.length:-1});
    })()
    """
    out = []
    for delay in (2, 4, 8):
        _t.sleep(delay)
        try:
            out.append("+%ss %s" % (delay, window.evaluate_js(js)))
        except Exception as e:
            out.append("+%ss ERR %s" % (delay, e))
    try:
        os.makedirs(cfg.LOCAL, exist_ok=True)
        with open(os.path.join(cfg.LOCAL, "diag.log"), "a", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
    except Exception:
        pass


def _confirm_exit():
    """自动同步开启时，退出前确认一次，避免用户以为还在后台同步。"""
    try:
        if not cfg.sync_cfg().get("enabled"):
            return True
    except Exception:
        return True
    try:
        r = ctypes.windll.user32.MessageBoxW(
            0, "自动同步已开启。\n退出软件后自动同步会停止。\n\n"
               "是：退出\n否：返回软件继续运行",
            APP_TITLE, 0x24)  # MB_YESNO | MB_ICONQUESTION
        return r == 6
    except Exception:
        return True


def main():
    mutex = acquire_single_instance()
    if mutex is None:
        return

    first_run = ensure_config()
    geom, win_w, win_h = load_geom()

    port = pick_port()
    server = chatvault.make_server(port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # 首次运行直接落到设置页，引导用户可视化填写配置
    # 带唯一版本参数，避免 WebView2 继续使用升级前的旧 HTML 缓存
    url = "http://127.0.0.1:%d/?v=%d" % (port, int(__import__("time").time()))
    if first_run:
        url += "&tab=settings"
    opts = dict(width=win_w, height=win_h, min_size=(900, 600),
                resizable=True, background_color="#ffffff")
    for k in ("x", "y"):
        if isinstance(geom.get(k), int):
            opts[k] = geom[k]
    window = webview.create_window(APP_TITLE, url, **opts)

    def on_closing():
        if not _confirm_exit():
            return False
        save_geom(window)
        try:
            server.shutdown()
        except Exception:
            pass

    try:
        window.events.closing += on_closing
    except Exception:
        pass

    webview.start(_diag_probe if os.environ.get("CHATVAULT_DIAG") == "1" else None,
                  window,
                  private_mode=False,
                  storage_path=os.path.join(cfg.LOCAL, "webview"))
    try:
        server.server_close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
