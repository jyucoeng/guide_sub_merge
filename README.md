# guide → Cloudflare Worker

把静态教程页 `guide.html`（含 13 张 `guide_files/*.webp` 截图）打包成一个**单文件 Worker**，
不依赖 Pages、R2、KV 等任何静态资源绑定。

## 为什么不是直接扔 HTML

`guide.html` 是用浏览器「另存为」存下来的，里面混进了浏览器扩展注入的东西：

| 垃圾 | 体积 | 处理 |
|---|---|---|
| uBlock 注入的隐藏广告规则（`<style>`，几百条 `display:none!important`） | **406 KB** | 整段删掉（与本站无关） |
| `<plasmo-csui>` 扩展宿主元素（含 shadow root） | 2 块 | 删掉 |
| 13 张 webp 截图（原 1600px 宽，共 2.4 MB；另 27 张旧图已删除） | 2.4 MB | 重编码到 ≤1280px / 质量 78 → **665 KB** |

清理后 HTML 只有 44.3 KB，页面自带的主题切换 / 阅读进度 / 目录高亮脚本（4.4 KB）原样保留。

## 构建

```bash
python3 build.py                 # 默认宽 ≤1280、质量 78 → worker.js 约 0.9 MB（gzip 约 0.7 MB）
python3 build.py --maxw 1600 --quality 85    # 想更清晰（体积更大）
```

依赖 `ffmpeg` 压图；没有 ffmpeg 时会原样打包（会大很多，可能超出免费版脚本体积上限）。

## 路由

| 路径 | 说明 |
|---|---|
| `GET /`（也接受 `/index.html`、`/guide.html`） | 教程页面，`cache-control: max-age=300` |
| `GET /img/<shot>.webp` | 内联在脚本里的截图，`max-age=31536000, immutable`；旧路径 `/guide_files/<shot>.webp` 等价 |
| `GET /healthz` | `{"ok":true,"images":13}` |
| 其他 | 404；非 GET/HEAD → 405 |

## 部署

```bash
npm i -g wrangler
wrangler login
wrangler deploy
```

免费版限制：脚本压缩后 ≤3 MB。当前 `worker.js` 0.92 MB（gzip 后约 0.67 MB），在免费额度内。

自定义域名：在 `wrangler.toml` 里打开 `routes` 段，或在 Dashboard → Workers → guide → Settings → Domains & Routes 绑定。
换域名后记得同步改 `guide.html` 里的 `<link rel="canonical">` 再重新 `build.py`。

## 本地验证

```bash
node --input-type=module -e "import w from './worker.js'; const r = await w.fetch(new Request('http://x/')); console.log(r.status, (await r.text()).length)"
```
