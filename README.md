# ChatVault · 本地聊天档案工作台

一个**完全本地运行**的聊天记录检索、阅读与管理工具。它读取你本机已经导出的
QQ / 微信 Markdown 归档，提供全文搜索、气泡阅读、联系人、群成员、收藏、统计、
增量同步和本地 AI 对话。

> 设计目标：把隐私留在自己电脑上。仓库里不含任何密钥、账号、私人路径或聊天记录。

![status](https://img.shields.io/badge/privacy-local--only-10a56b)
![python](https://img.shields.io/badge/python-3.10%2B-0099ff)

---

## 功能

| 模块 | 能力 |
|---|---|
| 搜索 | 全库全文检索、正则、按会话/日期/发送者筛选、分页、按会话分组 |
| 时间表 | 日历看每天消息量，点日期查看当天聊过的人和群，再点进完整对话 |
| 阅读 | 左侧搜索结果 + 右侧气泡阅读器，按天翻页、跳转日期、复制、导出 |
| 会话 | 全部会话列表、最近活跃排序、搜索、删除到回收站（两步确认） |
| 联系人 | 只显示真正私聊过的人；群成员按群单独查看，支持搜索与 CSV 导出 |
| 收藏 | 任意消息右键收藏，收藏页可跳回原文 |
| 统计 | 年度/月度消息量、会话 Top、发送者 Top、消息类型分布 |
| 导入 | 说明手机记录传到电脑的路径；检查已整理的 md 文件夹并复制到归档（不覆盖） |
| 同步 | 手动/自动同步 QQ、微信；可设间隔或每日时间、启动后同步、同步后更新索引 |
| 密钥与索引 | QQ/微信密钥状态与校验、全文索引增量更新/重建 |
| 设置 | 路径、模型服务、回收站恢复、数据体检、开机自启、外观与字体 |
| 对话 | 可选附带本地归档上下文，发送前强制脱敏 |

## 安全红线

- 服务**只监听 `127.0.0.1`**，局域网和外网都访问不到。
- 搜索、浏览、统计、收藏全部本地完成，不发任何网络请求。
- 对话功能在送出前对**用户输入、历史消息、归档上下文**三部分强制脱敏。
- 归档默认**只读**；删除会话是移动到回收站，不真删文件。
- 真实密钥与路径只存在 `.local/config.json`，已被 `.gitignore` 排除。

## 快速开始（源码模式）

需要 Python 3.10+。

```bash
# 1. 准备配置
mkdir .local
copy config.example.json .local\config.json

# 2. 填入归档路径（首次启动也可以在图形向导里填）
#    真实值只写在本机 .local/config.json

# 3. 启动
python chatvault.py            # 浏览器模式：http://127.0.0.1:8765
python chatvault_app.py        # 独立窗口模式（需 pywebview）
```

可选依赖：

```bash
pip install pywebview pyinstaller
pip install cryptography zstandard      # 同步/解密相关
pip install frida                       # 仅微信密钥抓取需要
```

## 配置

所有配置在 `.local/config.json`，字段说明见 `config.example.json`。常用项：

| 字段 | 说明 |
|---|---|
| `paths.qq_archive` / `wechat_archive` | 归档根目录（含 `私聊/`、`群聊/`） |
| `paths.qq_incr` / `wechat_incr` | 同步增量写入目录 |
| `paths.index_db` | 全文索引库（留空 = 程序目录 `.local/index.db`） |
| `paths.trash` | 删除会话的回收站 |
| `sync.*` | 自动同步开关、间隔/每日时间、目标平台、启动同步、同步后建索引 |
| `providers` | 任意 OpenAI 兼容模型服务（云端或本地 Ollama/LM Studio） |

## 打包 exe

```bash
python -m PyInstaller ChatVault.spec --noconfirm
```

发布版默认**不包含** QQ/微信解密工具（`tools/`）。需要同步功能时，请自行把
对应工具放到 `tools/qq-export-helper/src` 与 `tools/wechat4-db-decrypt/src`，
再重新打包。`.spec` 已做成可选：目录不存在时跳过，检索/阅读等功能仍可打包。

## 目录结构

```
chatvault.py            本地 HTTP 接口与检索/导出/统计
chatvault_index.py      SQLite FTS5 索引层
archive_parse.py        归档 Markdown 统一解析
chatvault.html          单页界面（无前端构建）
chatvault_app.py        pywebview 独立窗口启动器
local_config.py         配置加载与热重载
sanitize.py             对话脱敏
sync_qq.py / sync_wechat.py   增量同步（需自备解密工具）
conv_exclude.py         退役会话名单
group_alias.py / user_alias.py 会话键归一
ChatVault.spec          PyInstaller 配置
```

## 数据流

```
QQ/微信原始库 ──(解密工具，可选)──> Markdown 归档 ──> FTS5 索引 ──> 搜索/阅读/统计
                                      └──> 增量同步 ──> 归档
```

归档目录结构示例：

```
QQ/
  私聊/QQ123456789/2026-11.md
  群聊/群123456/2026-11.md
微信/
  私聊/昵称(wxid_xxx)/2026-11.md
```

## 已知限制

- 媒体消息（图片/文件）目前是占位文本，尚未与媒体文件建立映射。
- 自动同步只在软件运行期间生效；关闭软件即停止。
- 关闭窗口时会提示自动同步将停止。
- 微信归档没有“日”的概念，阅读器按整月/写入顺序展示。
- 发布版不含解密工具，首次使用同步前需按上文自行准备。

## 开发与自测

```bash
python -m py_compile chatvault.py chatvault_index.py archive_parse.py sync_qq.py sync_wechat.py
python chatvault.py --port 8765
curl http://127.0.0.1:8765/api/index/status
```

## 上传到 GitHub 前

运行仓库内的隐私自查脚本，生成只含发布文件的干净目录：

```bash
python prepare_github.py --target ..\ChatVault-Release
```

脚本会：

1. 只读取白名单文件；
2. 扫描真实密钥、账号、私人路径；
3. 任一命中即中止，不写文件；
4. 通过后生成可直接 `git init` 上传的目录。

**不要**直接上传含 `.local/`、`uid_map.json`、`group_members.json`、日志、
`tools/` 或聊天归档的目录。

## License

MIT，见 `LICENSE`。