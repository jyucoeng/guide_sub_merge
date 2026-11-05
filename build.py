#!/usr/bin/env python3
"""guide.html -> Cloudflare Worker

把 guide.html（外加 guide_files/*.webp 截图）打包成单文件 worker.js：
  * 清掉浏览器扩展注入的垃圾（<plasmo-csui> 宿主、uBlock 的隐藏广告规则，占了 415KB）
  * 截图重编码为更小的 webp（默认宽 1280 / 质量 78），减少体积
  * HTML 与图片都内联进 worker.js，Worker 只依赖运行时自带能力，无需静态资源绑定

用法：
    python3 build.py              # 生成 worker.js
    python3 build.py --maxw 1600 --quality 85   # 想更清晰就调大（体积随之变大）
"""

import argparse
import base64
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC_HTML = ROOT / "guide.html"
SRC_IMG = ROOT / "guide_files"
OUT_WORKER = ROOT / "worker.js"
OUT_HTML = ROOT / "dist" / "index.html"

# 这些 <style> 是浏览器扩展（uBlock）注入到页面里的隐藏广告规则，
# 与本站点毫无关系，纯属截图时被一起存了下来，直接丢弃。
AD_STYLE_MARKERS = ("display: none!important", "display:none!important")


def strip_extension_styles(html: str) -> tuple[str, int]:
    removed = 0

    def repl(m: re.Match) -> str:
        css = m.group(1)
        hits = sum(css.count(marker) for marker in AD_STYLE_MARKERS)
        # 广告隐藏规则成百上千条；页面自己的样式里不会出现这种密度
        if hits >= 3 and ":root" not in css:
            nonlocal removed
            removed += len(m.group(0))
            return ""
        return m.group(0)

    return re.sub(r"<style[^>]*>(.*?)</style>", repl, html, flags=re.S), removed


def clean_html(html: str) -> tuple[str, dict]:
    stats = {}
    # 扩展注入的自定义元素（含 shadow root 模板）
    html, n = re.subn(r"<plasmo-csui>.*?</plasmo-csui>", "", html, flags=re.S)
    stats["plasmo_blocks"] = n
    html, removed = strip_extension_styles(html)
    stats["ad_css_bytes"] = removed
    # 截图路径改成 /img/xxx.webp（Worker 的图片路由）
    html = html.replace("./guide_files/", "./img/").replace("guide_files/", "img/")
    # 去掉“saved from url”注释里带的原始 URL 分片，纯装饰
    html = re.sub(r"<!-- saved from url=\([^)]*\)[^>]*-->\s*", "", html)
    # 去掉没有 src 的空 <img>（原页面里的残留，会额外发一次对页面自身的请求）
    html, n = re.subn(r'<img(?![^>]*\bsrc=)[^>]*>', "", html)
    stats["empty_img"] = n
    return html, stats


def encode_images(maxw: int, quality: int) -> tuple[dict[str, str], dict[str, int], int]:
    if not SRC_IMG.is_dir():
        sys.exit(f"找不到图片目录：{SRC_IMG}")

    files = sorted(p for p in SRC_IMG.iterdir() if p.suffix.lower() == ".webp")
    if not files:
        sys.exit(f"{SRC_IMG} 里没有 webp 截图")

    tmp = ROOT / "dist" / "img"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)

    have_ffmpeg = shutil.which("ffmpeg") is not None
    if not have_ffmpeg:
        print("⚠️  没找到 ffmpeg，截图按原样打包（体积会大很多）", file=sys.stderr)

    data: dict[str, str] = {}
    sizes: dict[str, int] = {}
    raw_total = 0
    for src in files:
        dst = tmp / src.name
        if have_ffmpeg:
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(src),
                "-vf", f"scale='min({maxw},iw)':-1",
                "-quality", str(quality),
                str(dst),
            ]
            subprocess.run(cmd, check=True)
            if not dst.exists() or dst.stat().st_size == 0:
                shutil.copy2(src, dst)
        else:
            shutil.copy2(src, dst)
        blob = dst.read_bytes()
        data[src.name] = base64.b64encode(blob).decode("ascii")
        sizes[src.name] = len(blob)
        raw_total += len(blob)
    return data, sizes, raw_total


WORKER_TEMPLATE = """/**
 * guide.html 的 Cloudflare Worker 版本（由 build.py 生成，请勿手改）
 *
 *   GET /                  -> 教程页面
 *   GET /img/<shot>.webp   -> 内联在脚本里的截图（强缓存）
 *   GET /healthz           -> {{"ok":true}}  (JSON)
 *
 * 部署： npm i -g wrangler && wrangler deploy
 */
const HTML = {html};

const IMAGES = {images};

const IMAGE_BYTES = {sizes};

const HTML_HEADERS = {{
  "content-type": "text/html; charset=utf-8",
  "cache-control": "public, max-age=300",
  "x-content-type-options": "nosniff",
  "referrer-policy": "no-referrer-when-downgrade",
}};

function base64ToBytes(b64) {{
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}}

function notFound() {{
  return new Response("404 Not Found\\n", {{
    status: 404,
    headers: {{ "content-type": "text/plain; charset=utf-8" }},
  }});
}}

export default {{
  async fetch(request) {{
    const method = request.method;
    if (method !== "GET" && method !== "HEAD") {{
      return new Response("405 Method Not Allowed\\n", {{
        status: 405,
        headers: {{ allow: "GET, HEAD", "content-type": "text/plain; charset=utf-8" }},
      }});
    }}

    const url = new URL(request.url);
    let path = url.pathname;
    try {{
      path = decodeURIComponent(path);
    }} catch (_) {{}}

    if (path === "/healthz") {{
      return new Response(JSON.stringify({{ ok: true, images: Object.keys(IMAGES).length }}), {{
        headers: {{ "content-type": "application/json; charset=utf-8" }},
      }});
    }}

    if (path === "/" || path === "/index.html" || path === "/guide.html") {{
      return new Response(method === "HEAD" ? null : HTML, {{ headers: HTML_HEADERS }});
    }}

    const m = path.match(/^\\/(?:img|guide_files)\\/([A-Za-z0-9._-]+\\.webp)$/);
    if (m && Object.prototype.hasOwnProperty.call(IMAGES, m[1])) {{
      const name = m[1];
      const headers = {{
        "content-type": "image/webp",
        "cache-control": "public, max-age=31536000, immutable",
        "content-length": String(IMAGE_BYTES[name]),
        "x-content-type-options": "nosniff",
      }};
      return new Response(method === "HEAD" ? null : base64ToBytes(IMAGES[name]), {{ headers }});
    }}

    return notFound();
  }},
}};
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--maxw", type=int, default=1280, help="截图最大宽度（默认 1280）")
    ap.add_argument("--quality", type=int, default=78, help="webp 质量（默认 78）")
    args = ap.parse_args()

    html_raw = SRC_HTML.read_text(encoding="utf-8", errors="replace")
    html, stats = clean_html(html_raw)
    images, sizes, raw_total = encode_images(args.maxw, args.quality)

    worker = WORKER_TEMPLATE.format(
        html=json.dumps(html, ensure_ascii=False),
        images=json.dumps(images, ensure_ascii=False, indent=0).replace("\n", ""),
        sizes=json.dumps(sizes, ensure_ascii=False),
    )
    OUT_WORKER.write_text(worker, encoding="utf-8")

    # 同时产出一份「可直接静态托管」的页面：dist/index.html（图片在 dist/img/）
    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(html, encoding="utf-8")
    (OUT_HTML.parent / ".nojekyll").write_text("", encoding="utf-8")

    print(f"原 HTML           : {len(html_raw) / 1024:8.1f} KB")
    print(f"清理后 HTML       : {len(html) / 1024:8.1f} KB"
          f"（丢扩展垃圾 {stats['ad_css_bytes'] / 1024:.1f} KB，plasmo 块 {stats['plasmo_blocks']} 个）")
    print(f"截图 {len(images)} 张       : {raw_total / 1024:8.1f} KB（宽≤{args.maxw} 质量{args.quality}）")
    print(f"worker.js         : {OUT_WORKER.stat().st_size / 1024:8.1f} KB")
    print(f"static 版         : dist/index.html（{OUT_HTML.stat().st_size / 1024:.1f} KB）+ dist/img/ {len(images)} 张，"
          f"可直推 GitHub Pages")


if __name__ == "__main__":
    main()
