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
  * 点播:对每个仓按“原始 -> 备份一 -> 备份二 -> 备份三 -> 备份四”
    的顺序逐个试,要求返回合法 JSON 且 sites 是非空数组,第一个通过
    的就用它,并在 build-status.json 里记下这次用的是原始还是第几备份。
    备份地址是站上给每个仓配好的那套,不是猜出来的:统一是
    /tvbox/<仓名>.json,域名依次是 0.wudaozhe.net、0.12yue.de5.net、
    0.cdz.qzz.io、0.wdzb.eu.cc,路径和原始地址不同(例如饭太硬原始是
    /tv,备份是 /tvbox/饭太硬.json),所以不能靠换 host 推导,必须原样列出。
    X4菠菜园在站上有两组:普通组原始是 0.12yue.de5.net/5/x4.json,
    “站长推荐”组原始是 0.wudaozhe.net/5/x4.json,两组备份完全相同,
    脚本把它们合并成一个仓的两个原始候选,避免多仓里出现重复仓。
    5 个候选全部失败的仓直接从本次输出里去掉,并记进 build-status.json,
    不会因为一个仓挂了就让整个多仓文件失效。注意备份是站长 2026-10-27
    的快照,内容可能比原始旧一点,走备份总比整个仓丢掉好。
  * 直播:只抓“海量直播聚合接口”那一个聚合文件(它本身已由站长维护,
    内含 197 条左右的直播合集),按 url 去重,默认过滤名字含 🔞 / 成人
    的条目,原样保留 name/url/ua/source 字段,不擅自补 type。
    注意站上没有给这个聚合文件列备份一~四线,所以脚本对直播用的
    换 host 候选只是“同路径换备份域名”的推测镜像,不是站长列出的备份,
    只有返回合法 JSON 且 lives 非空才会被采用,并在 build-status.json
    里明确标为推测镜像;如果连推测镜像都失败,直播本次不输出,
    旧的 lives-clean.json 原样保留,不覆盖成空文件。
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

# 点播仓:名字 + 原始地址 + 站上配好的备份一~四线。
# 地址均取自 2026-10-27 站上列表原样复制,含中文域名/中文路径的也原样写,
# 抓取时脚本内部会做 IDNA/URL 编码。备份路径统一是 /tvbox/<仓名>.json,
# 和原始路径不同,不要靠换 host 推导,必须原样列出。
# X4菠菜园有两组原始地址(普通组 + 站长推荐组),备份完全相同,
# 这里用 originals 列表合并成一个仓,避免多仓里出现重复仓。
VOD_SOURCES = [
    {"name": "X4菠菜园",
     "originals": ["https://0.12yue.de5.net/5/x4.json",
                   "https://0.wudaozhe.net/5/x4.json"],
     "backups": ["https://0.wudaozhe.net/tvbox/X4菠菜园.json",
                 "https://0.12yue.de5.net/tvbox/X4菠菜园.json",
                 "https://0.cdz.qzz.io/tvbox/X4菠菜园.json",
                 "https://0.wdzb.eu.cc/tvbox/X4菠菜园.json"]},
    {"name": "饭太硬", "url": "http://www.饭太硬.net/tv",
     "backups": ["https://0.wudaozhe.net/tvbox/饭太硬.json",
                 "https://0.12yue.de5.net/tvbox/饭太硬.json",
                 "https://0.cdz.qzz.io/tvbox/饭太硬.json",
                 "https://0.wdzb.eu.cc/tvbox/饭太硬.json"]},
    {"name": "摸鱼", "url": "http://我不是.摸鱼儿.top",
     "backups": ["https://0.wudaozhe.net/tvbox/摸鱼.json",
                 "https://0.12yue.de5.net/tvbox/摸鱼.json",
                 "https://0.cdz.qzz.io/tvbox/摸鱼.json",
                 "https://0.wdzb.eu.cc/tvbox/摸鱼.json"]},
    {"name": "王二小", "url": "https://9280.kstore.vip/aiwex.json",
     "backups": ["https://0.wudaozhe.net/tvbox/王二小.json",
                 "https://0.12yue.de5.net/tvbox/王二小.json",
                 "https://0.cdz.qzz.io/tvbox/王二小.json",
                 "https://0.wdzb.eu.cc/tvbox/王二小.json"]},
    {"name": "潇洒", "url": "https://9877.kstore.space/sun.json",
     "backups": ["https://0.wudaozhe.net/tvbox/潇洒.json",
                 "https://0.12yue.de5.net/tvbox/潇洒.json",
                 "https://0.cdz.qzz.io/tvbox/潇洒.json",
                 "https://0.wdzb.eu.cc/tvbox/潇洒.json"]},
    {"name": "嗷呜", "url": "http://www.英格里希嗷呜.top/tv",
     "backups": ["https://0.wudaozhe.net/tvbox/嗷呜.json",
                 "https://0.12yue.de5.net/tvbox/嗷呜.json",
                 "https://0.cdz.qzz.io/tvbox/嗷呜.json",
                 "https://0.wdzb.eu.cc/tvbox/嗷呜.json"]},
    {"name": "X4更新专用接口", "url": "https://0.12yue.de5.net/0/666",
     "backups": ["https://0.wudaozhe.net/tvbox/X4更新专用接口.json",
                 "https://0.12yue.de5.net/tvbox/X4更新专用接口.json",
                 "https://0.cdz.qzz.io/tvbox/X4更新专用接口.json",
                 "https://0.wdzb.eu.cc/tvbox/X4更新专用接口.json"]},
    {"name": "X4菠菜pro", "url": "https://0.12yue.de5.net/5/x4pro.json",
     "backups": ["https://0.wudaozhe.net/tvbox/X4菠菜pro.json",
                 "https://0.12yue.de5.net/tvbox/X4菠菜pro.json",
                 "https://0.cdz.qzz.io/tvbox/X4菠菜pro.json",
                 "https://0.wdzb.eu.cc/tvbox/X4菠菜pro.json"]},
    {"name": "X4少儿频道", "url": "https://0.12yue.de5.net/5/tv少儿.json",
     "backups": ["https://0.wudaozhe.net/tvbox/X4少儿频道.json",
                 "https://0.12yue.de5.net/tvbox/X4少儿频道.json",
                 "https://0.cdz.qzz.io/tvbox/X4少儿频道.json",
                 "https://0.wdzb.eu.cc/tvbox/X4少儿频道.json"]},
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
    # 注意：safe 里必须含 %，否则已编码的路径（如直播聚合的
    # %E6%B5%B7…）会被二次编码成 %25E6…，服务器回 HTML 而非 JSON，
    # 直播抓取就会一直失败。原始中文字符仍会被 quote 编码。
    path = urllib.parse.quote(parts.path, safe="/:@-._~!$&'()*+,;=%")
    query = urllib.parse.quote(parts.query, safe="=&?/:@-._~!$'()*+,;%")
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, query, ""))


def fetch_text(url, timeout):
    req = urllib.request.Request(normalize_url(url), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    # 有的配置带 BOM,先去掉再解析。
    return raw.decode("utf-8-sig", errors="replace")


def fetch_json(url, timeout):
    return json.loads(fetch_text(url, timeout))


def candidate_urls(source):
    """返回 [(标签, 地址), ...],顺序就是尝试顺序,去重。

    标签:original / original2 / backup1 ~ backup4,用来记进报告,
    让使用者一眼看出这次用的是原始还是第几备份。
    """
    out = []
    originals = source.get("originals") or ([source["url"]] if source.get("url") else [])
    for i, url in enumerate(originals):
        label = "original" if i == 0 else "original%d" % (i + 1)
        if url and (label, url) not in out:
            out.append((label, url))
    for i, url in enumerate(source.get("backups", []), start=1):
        label = "backup%d" % i
        if url and (label, url) not in out:
            out.append((label, url))
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
        chosen_label = None
        errors = []
        for label, url in candidate_urls(source):
            try:
                data = fetch_json(url, timeout)
            except Exception as exc:  # noqa: BLE001 - 只要记失败原因,继续试下一个
                errors.append("%s: %s" % (url, exc))
                continue
            if isinstance(data, dict) and isinstance(data.get("sites"), list) \
                    and data["sites"]:
                chosen = url
                chosen_label = label
                break
            errors.append("%s: JSON 合法但 sites 为空或缺失" % url)
        if chosen:
            entries.append({"name": source["name"], "url": chosen})
            report.append({"name": source["name"], "url": chosen,
                           "used": chosen_label, "ok": True})
        else:
            first = (source.get("originals") or [source.get("url")])[0]
            report.append(
                {"name": source["name"], "url": first, "ok": False,
                 "errors": errors}
            )
    return entries, report


def live_candidate_urls():
    """直播聚合的候选地址:原始地址排第一,其余是同路径换备份域名的推测镜像。

    站上没有给这个文件列备份一~四线,所以换 host 的这几条不是站长列出的
    备份,只是推测,调用方必须在报告里标明,只有校验通过才会被采用。
    """
    out = [LIVE_AGGREGATION_URL]
    for host in BACKUP_HOSTS:
        url = _swap_host(LIVE_AGGREGATION_URL, host)
        if url not in out:
            out.append(url)
    return out


def build_lives(timeout):
    """返回 (清洗后的 lives 列表, 统计)。"""
    data = None
    chosen_url = None
    errors = []
    for url in live_candidate_urls():
        try:
            data = fetch_json(url, timeout)
        except Exception as exc:  # noqa: BLE001 - 记失败原因,继续试下一个
            errors.append("%s: %s" % (url, exc))
            continue
        raw_check = data.get("lives") if isinstance(data, dict) else None
        if isinstance(raw_check, list) and raw_check:
            chosen_url = url
            break
        errors.append("%s: JSON 合法但 lives 为空或缺失" % url)
        data = None
    if data is None:
        raise ValueError("直播聚合全部候选地址失败: %s" % "; ".join(errors))
    raw = data.get("lives")
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
        "chosen_url": chosen_url,
        # True 表示这次用的是换 host 推测镜像,不是站上列出的备份地址。
        "chosen_is_inferred_mirror": chosen_url != LIVE_AGGREGATION_URL,
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
            {"name": "海量直播聚合",
             "url": lives_stats["chosen_url"] if lives_stats else LIVE_AGGREGATION_URL})
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
        print("  %s %s [%s] -> %s" % ("OK " if item["ok"] else "FAIL", item["name"],
                                      item.get("used", "-"), item["url"]))
    if lives_stats:
        print("直播: 原始 %d 条,过滤成人向 %d 条,最终 %d 条"
              % (lives_stats["raw_count"], lives_stats["filtered_adult"],
                 lives_stats["final_count"]))
    print("已写入: %s" % args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
