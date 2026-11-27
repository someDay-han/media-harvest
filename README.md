# media_harvest

Pixabay 免费素材采集 CLI（sfx / music / image / video），中央素材库 + 许可留痕。

## 安装

```bash
git clone <repo-url> media-harvest && cd media-harvest
# 系统 Python 3.12 + uv（PEP 668，不往系统环境装包）
uv tool install --editable . --force
# 命令入口
~/.local/bin/mediaharvest --version
```

首次配置（可选）：`cp config.example.yaml config.yaml`。配置查找顺序：
`--config <path>` → `./config.yaml`（向上两级）→ `~/.config/media_harvest/config.yaml`。
不建配置文件也能跑，默认值见 `config.example.yaml` 注释。

## 用法

```bash
# 搜（四类素材同一套命令）
mediaharvest search whoosh --kind sfx        # 音效
mediaharvest search "cinematic loop" --kind music
mediaharvest search cat --kind image
mediaharvest search water --kind video
mediaharvest search "rain" --kind music --page 2

# 试听/查看（不落库）
mediaharvest open 1                          # 有图形会话：ffplay 流式播 CDN / xdg-open 看图
mediaharvest open 6578336                    # 无图形会话（SSH）：落盘到缓存预览目录，打印路径 + 直链

# 下载入中央库 ~/media/pixabay/（manifest.json 登记许可与来源）
mediaharvest download 1
mediaharvest download 620360 --to ./assets

# 翻库（离线）
mediaharvest list [--kind sfx]
mediaharvest show 620360

# 排障
mediaharvest doctor                          # 配置 → CDN → playwright → jina → 播放器 逐层诊断
mediaharvest providers
```

通用选项：`--json`（机器可读输出）、`--config <path>`、`--no-cache`。
退出码：0 成功 / 1 无结果 / 2 通道故障 / 3 用法错误。

## 运行环境（Linux）

| 组件 | 要求 |
|---|---|
| Python | ≥ 3.11（本机 3.12.3，PEP 668，用 uv 安装） |
| 网络 | `cdn.pixabay.com`、`r.jina.ai` 直连可达；`pixabay.com` 搜索页被 Cloudflare 拦（403），靠 Playwright 过 |
| 浏览器 | 本机 `google-chrome`（`channel="chrome"`）；没有则 `playwright install chromium` |
| 播放 | 可选。`ffplay`（ffmpeg）/ `mpv` / `vlc` 任一 + 图形会话；无图形会话时 `open` 自动落盘 |

状态文件（`last-search.json`）在 `~/.local/state/media_harvest/`，搜索缓存在 `~/.cache/media_harvest/`。
`open` 的预览文件在 `~/.cache/media_harvest/preview/`（同名覆盖，需手动清理）。

## 技术原理（简要）

- 音频（sfx/music）：搜索页 HTML 内嵌 `window.__BOOTSTRAP_URL__` → bootstrap JSON → `page.results[].sources.src` 给出 CDN 直链（256kbps CBR MP3，站点最高音质）
- 图片/视频：bootstrap JSON 端点被 Cloudflare 独立 challenge 拦截（403），改走**浏览器渲染后 DOM/SSR 提取**；视频直链由封面 URL 构造 `_large.mp4`
- CDN 直链（`cdn.pixabay.com/{audio,photo,video}/...`）免登录免 token，直连优先，失败再走代理
- 页面获取通道三级降级：本机 Chrome(Playwright, channel=chrome) → 直连 httpx → r.jina.ai 渲染代理
- 搜索结果缓存 24h（同时满足 Pixabay ToS 的缓存要求）；请求间隔 ≥1s

## 合规边界（必读）

- 本工具定位"按需搜索 → 挑选 → 单条下载"，**不做**整页/整站批量抓取（违反 Pixabay ToS）
- Pixabay Content License：免费商用、免署名、可修改；**禁止把原始文件 standalone 再分发**（打包传网盘/素材站倒卖不行）；嵌入自己作品（视频/文章）里用是允许的
- manifest 留痕（作者/page_url/许可）是为了版权可追溯，不是再分发授权

## 接口失效时的诊断手册（rea）

Pixabay 前端改版是主要长期风险（bootstrap 端点是会话相关 hash、类名是 CSS-module hash）。自救路径：

1. `mediaharvest doctor` 确认断在哪层（CDN 挂了是网络问题；playwright 层挂了大概率是站点改版/CF 策略变化）
2. 用 [rea-agents](https://github.com/morluto/rea)（已装，`rea --help`）逆向新接口：
   - `rea capture-browser-scenario`：Playwright 抓 pixabay 搜索页的网络请求（HAR），确认 bootstrap JSON 端点新形态
   - `rea inspect-web-network-capture`：分析 HAR，对照 `media_harvest/providers/pixabay/parse.py` 里的正则/字段路径，找漂移点
   - `rea analyze-web-bundle`：如果 HTML 里 `__BOOTSTRAP_URL__` 没了，从 JS bundle 里找新的数据注入变量名（历史上经历过 `__BOOTSTRAP_URL__`、`__PAGE__` 两代）
3. 修改 `parse.py` 的正则/字段 → 用真实录制页回归（见「测试」一节）

## 测试

项目暂未附带测试，当前回归口径为：

```bash
mediaharvest doctor
mediaharvest search whoosh --kind sfx --json
mediaharvest search cat --kind image
```

解析层（`parse.py`）是纯函数、无 IO，补测试只需喂真实页面的 HTML/JSON fixture。
