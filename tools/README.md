# tools/

## build_content_bundle.py

把 `content/docs` 打成 Android 端可增量更新的内容包。Vercel 构建时自动执行（见根目录 `vercel.json`），
产物落在 `public/laws/`，随站点一起发布。

```bash
python3 tools/build_content_bundle.py --src content/docs --out public/laws --base-url /laws/
```

产出：

| 文件 | 体积 | 说明 |
| --- | --- | --- |
| `manifest.json` | ~0.4 KB | 版本号 + 各 bundle 的 sha256，App 每次启动拉这个 |
| `index.json` | ~90 KB | 每个 md 的 sha256，App 做逐文件增量时才拉 |
| `text.zip` | ~3.0 MB | 全量文本，结构 `Laws/<分类>/<子类>/*.md` |
| `img-<n>.zip` | 0.4–22 MB | 可选（`--with-images`），按一级分类切分的图片卷 |

规则：

- `_index.md` 不打包（App 用目录树，不需要 Hugo 的分类页）
- 图片引用断链会让构建**失败**，避免把坏内容发上线
- zip 内时间戳固定，同内容重复构建产出字节一致，方便判断"内容是否真的变了"
- 内容版本号优先取 `CONTENT_VERSION` 环境变量，否则用北京时间当天日期（`20261112`）
