# 影视仓多仓聚合（GitHub Actions 版）

不用自己的服务器，定时、生成、提交全在 GitHub 上完成。

## 仓库里应该有什么

```
build_ysc.py
.github/workflows/build-ysc.yml
README.md  <- 本文件
output/    <- 由 Actions 自动生成并提交，不要手工改
  duocang.json
  lives-clean.json
  build-status.json
```

`build_ysc.py` 只用 Python 3 标准库。它不把所有点播站拼成一个单仓，
而是生成一份多仓索引：顶层只有 `urls`，每条只有 `name` + `url`，
各仓的 `spider`、解析、规则都原样保留。直播单独生成一份清洗后的
`lives` 文件（聚合文件按地址去重，默认过滤名字含 🔞 / 成人的条目）。

## 建仓步骤

1. 在 GitHub 新建一个**公开仓库**（Public）。影视仓要无 token 才能拉到
   地址，私有仓库的 raw 链接它打不开。输出里只有站上本来就公开的仓地址，
   没有任何密钥，但千万别把带真实节点密钥的 Clash 配置、私人订阅之类
   文件放进同一个仓库。
2. 把本压缩包解压后的全部文件上传到仓库根目录，保持上面的目录结构，
   尤其是 `.github/workflows/build-ysc.yml` 的路径不能错。
3. 打开仓库的 Actions 页，点 Run workflow 手动跑一次。
   第一次跑通后，`output/duocang.json` 就会出现在仓库里。
4. 以后每 6 小时自动跑一次，不用再管。

## 填进影视仓的地址

把 `<user>` 和 `<repo>` 换成你自己的，分支默认 `main`：

- 最及时：
  `https://raw.githubusercontent.com/<user>/<repo>/main/output/duocang.json`
- 国内一般更快，但有缓存，新提交后要等几分钟才生效：
  `https://cdn.jsdelivr.net/gh/<user>/<repo>@main/output/duocang.json`
- 备选，在 raw 地址前套 gh-proxy：
  `https://gh-proxy.org/https://raw.githubusercontent.com/<user>/<repo>/main/output/duocang.json`

填进影视仓：设置 → 配置地址，粘贴上面其中一个直链，不要填仓库网页地址。
电视版输长地址不方便，可以先转短链或二维码再扫码。

发布完先验证：先在浏览器里打开那个地址，确认能直接看到
`{"urls": [...]}` 的 JSON 内容，再填进影视仓；如果浏览器都打不开，
影视仓也拉不到。

## 出问题先看哪里

- Actions 页某次运行失败：点进去看 `Build duocang and lives` 那一步的日志。
  某几个仓挂了是正常的，脚本会把它们从本次输出去掉并记进
  `output/build-status.json`；只有点播仓全部失败时，工作流才会失败、
  不提交，旧输出原样保留，下次定时再试。
- 定时任务没跑：GitHub 的定时任务在高负载时会延迟；仓库连续 60 天
  没有任何活动时，定时任务会被 GitHub 暂停，去 Actions 页重新启用、
  手动跑一次即可。
- jsDelivr 地址还是旧内容：等几分钟让缓存过期，或者先用 raw 地址验证
  新内容已经提交上去了。
- 影视仓导入后某个仓打不开：先看 `output/build-status.json` 里那个仓
  本次实际选了哪个地址，再在影视仓里单独切到那个仓试。
