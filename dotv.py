# -*- coding: utf-8 -*-
"""
dotv (tmxk) 直播源抓取脚本（含可用性检测）
==========================================
数据来源: https://dotv.tmxk.kdns.fr/?key=LEON888
接口是 TVBox 风格 JSON: list[].vod_play_url 形如 "线路名$URL#线路名$URL"

功能:
1. 抓取全部频道 (vod_id 以 all_ 开头的有效条目, 自动跳过引流条目)
2. 频道名规范化: "CCTV-1 综合" -> "CCTV1" (其他频道名不变)
3. 并发探测每条线路 (Range 只拉 2KB, 200/206 = 可用, 自动跟随重定向)
4. 只把可用线路写进输出文件, 格式: 分组,#genre# + 频道名,URL
   分组规则: 央视(CCTV/CGTN开头) / 卫视(名字含"卫视") / 其他
5. 输出文件最前面固定写入 "花卷频道 + 温馨提示" 两行 (HEADER_LINES)

用法:
    python dotv.py                # 输出 tv.txt 到脚本同目录 (GitHub Actions 用这个)
    python dotv.py 输出路径.txt
    python dotv.py --no-check     # 跳过检测, 全部线路直接输出

仅用 Python 标准库。
"""

import json
import os
import re
import ssl
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

API_URL = "https://dotv.tmxk.kdns.fr/?key=LEON888"
UA = "okhttp/4.10.0"
DEFAULT_OUT = "tv.txt"

# 输出文件最前面的固定头部 (温馨提示图片行)
HEADER_LINES = [
    "花卷频道,#genre#",
    "温馨提示,https://i.imgs.ovh/2026/10/24/f0b309a21be2be1b6bf9e8dc0a7aca92.jpg",
]

CHECK_WORKERS = 64    # 探测并发
CHECK_TIMEOUT = 7     # 探测超时(秒)

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
        return resp.read()


def normalize_name(name):
    """CCTV-1 综合 -> CCTV1, CCTV-5+ 体育赛事 -> CCTV5+, CCTV-4K 超高清 -> CCTV4K
    规则: CCTV- 后的编号(数字/+/K)提取拼接, 丢弃编号后的描述文字; 其他频道名不动"""
    m = re.match(r"^CCTV-(\d+(?:\+|K)?)(?:\s+.*)?$", name)
    if m:
        return f"CCTV{m.group(1)}"
    return name


def fetch_channels():
    """拉取接口并解析为 [(频道名, [线路URL...]), ...]"""
    data = json.loads(http_get(API_URL).decode("utf-8"))
    channels = []
    for item in data.get("list", []):
        # 只取正片频道, 跳过引流/公告类条目
        if not str(item.get("vod_id", "")).startswith("all_"):
            continue
        name = normalize_name(str(item.get("vod_name", "")).strip())
        if not name:
            continue
        urls = []
        for seg in str(item.get("vod_play_url") or "").split("#"):
            if "$" not in seg:
                continue
            u = seg.split("$", 1)[1].strip()
            if u.startswith(("http://", "https://")):
                urls.append(u)
        if urls:
            channels.append((name, urls))
    return channels


def probe(url):
    """探测单个地址: 200/206 = 可用 (urllib 自动跟随重定向)"""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Range": "bytes=0-2047", "Connection": "close"})
        with urllib.request.urlopen(req, timeout=CHECK_TIMEOUT, context=CTX) as resp:
            if resp.status in (200, 206):
                resp.read(1024)
                return True, "OK"
            return False, f"HTTP {resp.status}"
    except Exception as e:
        return False, (str(e) or type(e).__name__)[:60]


def check_all(entries):
    """并发探测, 返回 {url: (ok, reason)}"""
    uniq = sorted({u for _, u in entries})
    print(f"[检测] 共 {len(entries)} 条线路 (去重 {len(uniq)} 个地址), "
          f"并发 {CHECK_WORKERS}, 超时 {CHECK_TIMEOUT}s ...")
    cache = {}
    done = 0
    with ThreadPoolExecutor(max_workers=CHECK_WORKERS) as pool:
        futs = {pool.submit(probe, u): u for u in uniq}
        for fut in as_completed(futs):
            cache[futs[fut]] = fut.result()
            done += 1
            if done % 100 == 0:
                print(f"  进度 {done}/{len(uniq)}")
    return cache


def group_of(name):
    if re.match(r"(CCTV|CGTN)", name):
        return "央视"
    if "卫视" in name:
        return "卫视"
    return "其他"


def write_txt(groups, fh):
    """groups: {分组: [(频道名, URL)]} 按指定顺序输出 (最前面带固定头部)"""
    for line in HEADER_LINES:
        fh.write(line + "\n")
    fh.write("\n")
    for g in ["央视", "卫视", "其他"]:
        rows = groups.get(g)
        if not rows:
            continue
        fh.write(f"{g},#genre#\n")
        for name, url in rows:
            fh.write(f"{name},{url}\n")
        fh.write("\n")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    no_check = "--no-check" in sys.argv
    out = args[0] if args else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), DEFAULT_OUT)
    out_dir = os.path.dirname(out) or "."
    out_report = os.path.join(out_dir, "dotv失效线路报告.txt")

    print(f"[1/2] 拉取接口: {API_URL}")
    channels = fetch_channels()
    total_lines = sum(len(us) for _, us in channels)
    print(f"      频道 {len(channels)} 个, 线路 {total_lines} 条")

    if no_check:
        groups = {}
        for name, urls in channels:
            g = group_of(name)
            for u in urls:
                groups.setdefault(g, []).append((name, u))
        with open(out, "w", encoding="utf-8") as f:
            write_txt(groups, f)
        print(f"\n已跳过检测, 全部输出 -> {out}")
        return

    print("[2/2] 检测线路可用性 ...")
    entries = [(name, u) for name, urls in channels for u in urls]
    cache = check_all(entries)

    alive_groups, dead_groups = {}, {}
    n_alive = n_dead = 0
    for name, urls in channels:
        g = group_of(name)
        for u in urls:
            ok, reason = cache.get(u, (False, "未检测"))
            if ok:
                alive_groups.setdefault(g, []).append((name, u))
                n_alive += 1
            else:
                dead_groups.setdefault(g, []).append((name, u, reason))
                n_dead += 1

    with open(out, "w", encoding="utf-8") as f:
        write_txt(alive_groups, f)

    print(f"\n总计: 可用 {n_alive} / 失效 {n_dead}")
    for g in ["央视", "卫视", "其他"]:
        a = len(alive_groups.get(g, []))
        d = len(dead_groups.get(g, []))
        if a + d:
            print(f"  [{g}] 可用 {a} / 失效 {d}")
    print(f"输出(只含可用线路) -> {out}")

    # 失效明细
    with open(out_report, "w", encoding="utf-8") as f:
        f.write("dotv 直播源可用性检测报告\n")
        f.write(f"来源: {API_URL}\n")
        f.write(f"探测并发 {CHECK_WORKERS} | 超时 {CHECK_TIMEOUT}s\n")
        f.write("注意: HTTP 探测仅验证地址可达, 不代表画面内容一定正常。\n\n")
        for g, rows in dead_groups.items():
            f.write(f"===== {g} ===== 失效 {len(rows)} 条\n")
            for name, url, reason in rows:
                f.write(f"[失效] {name}  ({reason})\n        {url}\n")
            f.write("\n")
    print(f"失效明细 -> {out_report}")


if __name__ == "__main__":
    main()
