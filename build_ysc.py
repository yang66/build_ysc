#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
影视仓多仓聚合脚本 build_ysc.py

目标:把 tvbox.jierry123.eu.cc 上那批分散的线路,整合成影视仓能直接导入的
两份文件,而不是把所有点播站硬拼进一个单仓配置里。

为什么不做深度合并?
  * 每个单仓只有一个 spider 字段,而且很多是相对路径(例如 ./lib/xs.jar),
    不同仓的 jar 互不兼容,拼在一起会先坏掉 type 3 爬虫站。
  * 不同仓的站点 key 会撞,资源站也高度重合,合并只是堆重复。
  * 多仓文件本身只是一个索引,影视仓按顶层键区分:
    有 sites -> 单仓;顶层只有 urls -> 多仓订阅。

输出(写到 --out-dir,原子替换,失败不覆盖旧文件):
  * duocang.json       多仓订阅,顶层只有 urls,每条只有 name + url
                       导入路径:影视仓 设置 -> 配置地址,粘贴这个文件的直链
  * lives-clean.json   清洗后的直播聚合,顶层只有 lives
                       用法二选一:
                       (1) 作为 duocang.json 里“海量直播聚合”那一条,切仓看直播;
                       (2) 走影视仓的自定义直播源单独导入(版本不同入口名字可能不同)
  * build-status.json  本次构建的报告:时间、每个仓选中的实际地址、失败项、
                       直播去重/过滤前后的条数。不给影视仓用,只给人看。

抓取策略(只做健康检查,不改写原配置):
  * 点播:对每个仓先试原始地址,要求返回合法 JSON 且 sites 是非空数组;
    失败时,对原始 host 本来就在 4 个备份域名里的仓,再试把 host 换成
    其余备份域名(0.wudaozhe.net、0.12yue.de5.net、0.cdz.qzz.io、
    0.wdzb.eu.cc),同样要校验通过才算数。原始 host 不在备份域名里
    的仓不换 host,避免抓到别人家的配置张冠李戴。
    全部失败的仓直接从本次输出里去掉,并记进 build-status.json,不会
    因为一个仓挂了就让整个多仓文件失效。
  * 直播:只抓“海量直播聚合接口”那一个聚合文件(它本身已由站长维护,
    内含 197 条左右的直播合集),按 url 去重,默认过滤名字含 🔞 / 成人
    的条目,原样保留 name/url/ua/source 字段,不擅自补 type。
  * 注意:本脚本只校验“配置能拉到、JSON 合法、sites/lives 非空”,
    不逐条验证播放,最终能不能播仍需在影视仓里实测。

依赖:仅 Python 3 标准库,无需 pip。定时跑法见 README.md。
来源快照:2026-10-27,取自 tvbox.jierry123.eu.cc 的点播/直播列表。
新增仓时,在下面 VOD_SOURCES 里加一行即可,不要改脚本逻辑。
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# 站上 4 个互为备份的域名(点播/直播的“备份一线~四线”用的就是它们)。
BACKUP_HOSTS = [
    "0.wudaozhe.net",
    "0.12yue.de5.net",
    "0.cdz.qzz.io",
    "0.wdzb.eu.cc",
]

# 点播仓:名字 + 原始地址。地址均取自 2026-10-27 站上列表,原样保留,
# 含中文域名的也原样写,抓取时脚本内部会做 IDNA/URL 编码。
VOD_SOURCES = [
    {"name": "X4菠菜园", "url": "https://0.12yue.de5.net/5/x4.json"},
    {"name": "饭太硬", "url": "http://www.饭太硬.net/tv"},
    {"name": "摸鱼", "url": "http://我不是.摸鱼儿.top"},
    {"name": "王二小", "url": "https://9280.kstore.vip/aiwex.json"},
    {"name": "潇洒", "url": "https://9877.kstore.space/sun.json"},
    {"name": "嗷呜", "url": "http://www.英格里希嗷呜.top/tv"},
    {"name": "X4更新专用接口", "url": "https://0.12yue.de5.net/0/666"},
    {"name": "X4菠菜pro", "url": "https://0.12yue.de5.net/5/x4pro.json"},
    {"name": "X4少儿频道", "url": "https://0.12yue.de5.net/5/tv少儿.json"},
]

# 直播聚合:中文路径在部分抓取工具里会 400,必须用编码后的路径。
# 原始路径是 /tvbox/海量直播聚合接口.json,下面是编码版。
LIVE_AGGREGATION_URL = (
    "https://0.wudaozhe.net/tvbox/"
    "%E6%B5%B7%E9%87%8F%E7%9B%B4%E6%92%AD%E8%81%9A%E5%90%88%E6%8E%A5%E5%8F%A3.json"
)

# 站上直播列表里单独的 Guovin m3u,聚合文件里不一定有,补一条。
EXTRA_LIVES = [
    {
        "name": "Guovin",
        "url": "https://cdn.jsdelivr.net/gh/Guovin/iptv-api@gd/output/result.m3u",
    },
]

ADULT_MARKERS = ("🔞", "成人")
USER_AGENT = "Mozilla/5.0 (compatible; ysc-duocang-builder/1.0)"


def normalize_url(url):
    """把含中文域名/中文路径的 URL 转成可请求的 ASCII 形式。

    只编码 host(IDNA)和 path/query 中的非 ASCII 字符,保留 :/?&= 等分隔符。
    """
    parts = urllib.parse.urlsplit(url.strip())
    host = parts.hostname or ""
    try:
        host_ascii = host.encode("idna").decode("ascii")
    except Exception:
        host_ascii = host
    netloc = host_ascii
    if parts.port:
        netloc += ":%d" % parts.port
    if parts.username:
        netloc = parts.username + "@" + netloc
    path = urllib.parse.quote(parts.path, safe="/:@-._~!$&'()*+,;=")
    query = urllib.parse.quote(parts.query, safe="=&?/:@-._~!$'()*+,;")
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, query, ""))


def fetch_text(url, timeout):
    req = urllib.request.Request(normalize_url(url), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    # 有的配置带 BOM,先去掉再解析。
    return raw.decode("utf-8-sig", errors="replace")


def fetch_json(url, timeout):
    return json.loads(fetch_text(url, timeout))


def candidate_urls(source_url):
    """原始地址 + host 换成 4 个备份域名的变体,原始地址排第一,去重。

    只有原始 host 本来就在备份域名集合里时才换 host:站上的备份线路
    是“同一条配置在 4 个备份域名下各有一份”,换 host 才有意义。
    对饭太硬、王二小这类本来就不在备份域名上的仓,换 host 抓到的
    可能是别人家的配置,校验通过也会张冠李戴,所以不换。
    """
    out = [source_url]
    host = (urllib.parse.urlsplit(source_url).hostname or "").lower()
    if host in BACKUP_HOSTS:
        for new_host in BACKUP_HOSTS:
            url = _swap_host(source_url, new_host)
            if url not in out:
                out.append(url)
    return out


def _swap_host(url, new_host):
    parts = urllib.parse.urlsplit(url)
    if not parts.hostname:
        return url
    netloc = new_host
    if parts.port:
        netloc += ":%d" % parts.port
    return urllib.parse.urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, "")
    )


def build_vod(timeout):
    """返回 (多仓条目列表, 报告列表)。"""
    entries = []
    report = []
    for source in VOD_SOURCES:
        chosen = None
        errors = []
        for url in candidate_urls(source["url"]):
            try:
                data = fetch_json(url, timeout)
            except Exception as exc:  # noqa: BLE001 - 只要记失败原因,继续试下一个
                errors.append("%s: %s" % (url, exc))
                continue
            if isinstance(data, dict) and isinstance(data.get("sites"), list) \
                    and data["sites"]:
                chosen = url
                break
            errors.append("%s: JSON 合法但 sites 为空或缺失" % url)
        if chosen:
            entries.append({"name": source["name"], "url": chosen})
            report.append({"name": source["name"], "url": chosen, "ok": True})
        else:
            report.append(
                {"name": source["name"], "url": source["url"], "ok": False,
                 "errors": errors}
            )
    return entries, report


def build_lives(timeout):
    """返回 (清洗后的 lives 列表, 统计)。"""
    data = fetch_json(LIVE_AGGREGATION_URL, timeout)
    raw = data.get("lives") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        raise ValueError("直播聚合文件里没有非空 lives 数组")
    total = len(raw)
    seen = set()
    cleaned = []
    filtered_adult = 0
    for item in raw:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        name = str(item.get("name", ""))
        if any(marker in name for marker in ADULT_MARKERS):
            filtered_adult += 1
            continue
        key = normalize_url(str(item["url"]))
        if key in seen:
            continue
        seen.add(key)
        # 原样保留站长给的字段,不补 type、不改 ua/source。
        cleaned.append({k: item[k] for k in ("name", "url", "ua", "source")
                        if k in item})
    for extra in EXTRA_LIVES:
        if normalize_url(extra["url"]) not in seen:
            seen.add(normalize_url(extra["url"]))
            cleaned.append(dict(extra))
    return cleaned, {
        "aggregation_url": LIVE_AGGREGATION_URL,
        "raw_count": total,
        "filtered_adult": filtered_adult,
        "final_count": len(cleaned),
    }


def write_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成影视仓多仓订阅 + 清洗后直播文件")
    parser.add_argument("--out-dir", default="output",
                        help="输出目录(默认 ./output,需由 Web 服务器对外提供直链)")
    parser.add_argument("--timeout", type=int, default=15,
                        help="单个请求超时秒数(默认 15)")
    parser.add_argument("--skip-lives", action="store_true",
                        help="只生成点播多仓,不抓直播聚合")
    args = parser.parse_args(argv)

    os.makedirs(args.out_dir, exist_ok=True)
    vod_entries, vod_report = build_vod(args.timeout)
    if not vod_entries:
        print("点播仓全部校验失败,本次不覆盖旧输出:", file=sys.stderr)
        for item in vod_report:
            print("  - %s" % item["name"], file=sys.stderr)
        return 1

    lives = []
    lives_stats = None
    if not args.skip_lives:
        try:
            lives, lives_stats = build_lives(args.timeout)
        except Exception as exc:  # noqa: BLE001 - 直播失败不阻塞点播输出
            print("直播聚合抓取失败,本次只输出点播多仓: %s" % exc,
                  file=sys.stderr)

    # 多仓文件顶层只能有 urls,不要往里面塞时间戳等额外字段,
    # 否则影视仓可能无法识别为多仓订阅。
    duocang = {"urls": list(vod_entries)}
    if lives:
        duocang["urls"].append(
            {"name": "海量直播聚合", "url": LIVE_AGGREGATION_URL})
        write_atomic(os.path.join(args.out_dir, "lives-clean.json"),
                     {"lives": lives})

    write_atomic(os.path.join(args.out_dir, "duocang.json"), duocang)
    status = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "vod": vod_report,
        "vod_ok_count": len(vod_entries),
        "lives": lives_stats,
    }
    write_atomic(os.path.join(args.out_dir, "build-status.json"), status)

    print("点播仓成功 %d/%d" % (len(vod_entries), len(VOD_SOURCES)))
    for item in vod_report:
        print("  %s %s -> %s" % ("OK " if item["ok"] else "FAIL", item["name"],
                                 item["url"]))
    if lives_stats:
        print("直播: 原始 %d 条,过滤成人向 %d 条,最终 %d 条"
              % (lives_stats["raw_count"], lives_stats["filtered_adult"],
                 lives_stats["final_count"]))
    print("已写入: %s" % args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
