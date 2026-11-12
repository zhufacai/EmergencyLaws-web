#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 content/docs 打成 App 可增量更新的内容包。

产物（默认输出到 public/laws/）：
    manifest.json   版本、每个 md 的 sha256、bundle 清单
    text.zip        Laws/<分类>/<子类>/*.md 全量文本包
    img-<n>.zip     可选：按一级分类切分的图片卷（--with-images）

设计约束：
  * 只读 content/docs，不修改源文件
  * 同内容重复构建产出字节一致的 zip（固定时间戳），便于判断"是否真的变了"
  * 图片引用断链会让构建失败，防止把坏内容发到线上
  * 只用标准库，Vercel 构建镜像可直接跑

用法：
    python3 tools/build_content_bundle.py --src content/docs --out public/laws

    # 需要连图片一起发（体积大，默认关闭）
    python3 tools/build_content_bundle.py --src content/docs --out public/laws --with-images
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 内容包内的根目录名，必须与 Android 端 assets/Laws 的约定一致
LAW_ROOT = "Laws"
IMAGE_DIRS = ("images",)
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
SKIP_MD = {"_index.md"}

# 固定时间戳，保证同内容多次构建的 zip 字节一致
FIXED_ZIP_TIME = (2020, 1, 1, 0, 0, 0)

IMG_MD = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
IMG_HTML = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)


def log(msg: str) -> None:
    print(f"[content-bundle] {msg}", flush=True)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def iter_markdown(root: Path):
    for path in sorted(root.rglob("*.md")):
        if path.name in SKIP_MD:
            continue
        yield path


def resolve_version(explicit: str | None) -> int:
    """内容版本号，保证单调递增，便于 App 只做数字比较。

    优先级：--version > CONTENT_VERSION 环境变量 > git 最新提交时间(yyyymmddHHMM) > 当前时间。
    """
    if explicit:
        return int(explicit)
    env = os.environ.get("CONTENT_VERSION")
    if env:
        return int(env)
    tz = timezone(timedelta(hours=8))
    try:
        import subprocess

        ts = subprocess.check_output(
            ["git", "log", "-1", "--format=%ct"],
            cwd=Path(__file__).resolve().parent.parent,
            text=True, stderr=subprocess.DEVNULL).strip()
        if ts.isdigit():
            return int(datetime.fromtimestamp(int(ts), tz).strftime("%Y%m%d%H%M"))
    except Exception:
        pass
    return int(datetime.now(tz).strftime("%Y%m%d%H%M"))


def check_image_refs(md_files, src_root: Path):
    """校验每篇 md 里的相对图片引用都能解析到真实文件。"""
    broken = []
    total_refs = 0
    for md in md_files:
        text = md.read_text(encoding="utf-8", errors="replace")
        refs = IMG_MD.findall(text) + IMG_HTML.findall(text)
        for ref in refs:
            ref = ref.strip()
            if not ref or ref.startswith(("http://", "https://", "data:", "/")):
                continue
            total_refs += 1
            target = (md.parent / ref).resolve()
            if not target.is_file():
                broken.append((str(md.relative_to(src_root)), ref))
    return total_refs, broken


def build_text_zip(md_files, src_root: Path) -> tuple[bytes, dict]:
    """把 md 打包成 Laws/<相对路径>，同时返回每个文件的元信息。"""
    files_meta: dict[str, dict] = {}
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for md in md_files:
            rel = md.relative_to(src_root).as_posix()
            arcname = f"{LAW_ROOT}/{rel}"
            data = md.read_bytes()
            info = zipfile.ZipInfo(arcname, date_time=FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
            files_meta[arcname] = {"sha256": sha256_bytes(data), "size": len(data), "bundle": "text"}
    return buf.getvalue(), files_meta


def build_image_volumes(md_files, src_root: Path) -> tuple[dict, dict, dict]:
    """按一级分类打图片卷；只打被 md 引用到的图片。返回 (bundles, files_meta, blobs)。"""
    import io

    referenced: dict[str, set] = {}
    for md in md_files:
        text = md.read_text(encoding="utf-8", errors="replace")
        for ref in IMG_MD.findall(text) + IMG_HTML.findall(text):
            ref = ref.strip()
            if not ref or ref.startswith(("http://", "https://", "data:", "/")):
                continue
            target = (md.parent / ref).resolve()
            if target.is_file():
                referenced.setdefault(target.relative_to(src_root).parts[0], set()).add(target)

    bundles: dict[str, dict] = {}
    files_meta: dict[str, dict] = {}
    blobs: dict[str, bytes] = {}
    for index, (group, paths) in enumerate(sorted(referenced.items())):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            for path in sorted(paths):
                data = path.read_bytes()
                arcname = f"{LAW_ROOT}/{path.relative_to(src_root).as_posix()}"
                info = zipfile.ZipInfo(arcname, date_time=FIXED_ZIP_TIME)
                info.compress_type = zipfile.ZIP_STORED  # 图片已压缩，再压没意义
                info.external_attr = 0o644 << 16
                zf.writestr(info, data)
                files_meta[arcname] = {"sha256": sha256_bytes(data), "size": len(data),
                                       "bundle": f"img-{index}"}
        blob = buf.getvalue()
        blobs[f"img-{index}"] = blob
        bundles[f"img-{index}"] = {"file": f"img-{index}.zip", "size": len(blob),
                                   "sha256": sha256_bytes(blob), "group": group,
                                   "count": len(paths)}
    return bundles, files_meta, blobs


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 App 法规内容包")
    parser.add_argument("--src", default="content/docs", help="md 源目录")
    parser.add_argument("--out", default="public/laws", help="产物输出目录")
    parser.add_argument("--base-url", default="/laws/", help="manifest 中的下载基地址")
    parser.add_argument("--version", default=None, help="内容版本号，默认取 CONTENT_VERSION 或当天日期")
    parser.add_argument("--min-app-version", default="20260420", help="低于该 versionCode 的 App 不下发")
    parser.add_argument("--with-images", action="store_true", help="同时产出图片卷（体积大）")
    args = parser.parse_args()

    src_root = Path(args.src).resolve()
    if not src_root.is_dir():
        log(f"源目录不存在: {src_root}")
        return 2
    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    md_files = list(iter_markdown(src_root))
    if not md_files:
        log(f"没有找到 md 文件: {src_root}")
        return 2

    log(f"源目录 {src_root}，共 {len(md_files)} 篇 md")

    total_refs, broken = check_image_refs(md_files, src_root)
    log(f"图片引用 {total_refs} 处，断链 {len(broken)} 处")
    if broken:
        for path, ref in broken[:20]:
            log(f"  断链: {path} -> {ref}")
        log("存在断链，终止构建（避免发布坏内容）")
        return 1

    text_zip, text_meta = build_text_zip(md_files, src_root)
    (out_root / "text.zip").write_bytes(text_zip)
    log(f"text.zip  {len(text_zip) / 1048576:.2f} MB  sha256={sha256_bytes(text_zip)[:16]}…")

    bundles = {"text": {"file": "text.zip", "size": len(text_zip),
                        "sha256": sha256_bytes(text_zip)}}
    files_meta = dict(text_meta)

    if args.with_images:
        img_bundles, img_meta, img_blobs = build_image_volumes(md_files, src_root)
        bundles.update(img_bundles)
        files_meta.update(img_meta)
        for name, blob in img_blobs.items():
            (out_root / img_bundles[name]["file"]).write_bytes(blob)
            log(f"{img_bundles[name]['file']}  {len(blob) / 1048576:.2f} MB  "
                f"({img_bundles[name]['group']}, {img_bundles[name]['count']} 张)")

    version = resolve_version(args.version)
    # manifest 保持小体积（App 每次启动都要拉），逐文件校验信息拆到 index.json 按需下载
    manifest = {
        "schema": 1,
        "contentVersion": version,
        "generatedAt": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "minAppVersion": int(args.min_app_version),
        "base": args.base_url,
        "docs": len(md_files),
        "bundles": bundles,
        "index": "index.json",
    }
    (out_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")

    index = {
        "schema": 1,
        "contentVersion": version,
        "files": files_meta,
    }
    (out_root / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8")

    manifest_size = (out_root / "manifest.json").stat().st_size
    index_size = (out_root / "index.json").stat().st_size
    log(f"manifest.json {manifest_size / 1024:.1f} KB  contentVersion={version} "
        f"bundles={list(bundles)}")
    log(f"index.json    {index_size / 1024:.1f} KB  files={len(files_meta)}")
    log(f"输出目录 {out_root}")
    log("完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
