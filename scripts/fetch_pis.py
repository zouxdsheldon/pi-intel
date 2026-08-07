#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PI 情报数据层:语料抽取 → Europe PMC 作品集 → 方向指纹/轨迹/竞争互补 → data/pis.json

数据源说明(为什么是 Europe PMC 而不是别的):
  · Google Scholar   —— 无官方 API,抓取违反 ToS,域名不可达 → 不用
  · OpenAlex         —— 最合适,但需 API key;用户已明确拒绝授权 → 跳过
  · 实验室网站爬虫    —— 任意院校域名不在网络白名单 → 抓不到
  · 大模型生成分析    —— 会产出查不到出处的句子,违反本站离线可核验原则 → 不用
  · Europe PMC       —— 免 key、已白名单、返回作者列表+单位+期刊 → 唯一可信来源

单写者原则:本脚本只写 data/pis.json,绝不回写 papers.json。

用法:
    python3 scripts/fetch_pis.py --limit 60 --budget 900
"""
import argparse, json, math, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
sys.path.insert(0, HERE)

from pi_extract import build_candidates, parse_name, name_key, extract_inst, guess_region  # noqa: E402

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
UA = {"User-Agent": "pi-intel-site/1.0 (+https://github.com/zouxdsheldon)"}
T0 = time.time()
SURFREQ = {}
FAIL = Counter()


def http_json(url, timeout=25, tries=3):
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:                      # 连接被掐、超时、502 都归这里
            last = e
            time.sleep(1.2 * (k + 1))
    raise last


# ---------------------------------------------------------------- 领域约束
def domain_clause(cfg, max_terms=14):
    """用 interests.json 的 core 词构造领域过滤子句 —— 这样 'Zhang X' 只返回
    本领域论文,而不是全库同名者的所有文章。同时 hitCount(无领域词)与
    领域内命中数之比,本身就是一个可显示的同名/跨领域指标。"""
    terms = []
    for it in cfg["interests"]:
        for t in it["core"][:3]:
            if len(t) > 3 and t not in terms:
                terms.append(t)
    terms = terms[:max_terms]
    return "(" + " OR ".join(f'"{t}"' for t in terms) + ")", terms


def epmc_author(name, dom_clause, page_size=100, max_records=200):
    """取一位作者的领域内作品集。返回 (records, hit_domain, hit_total, err)。"""
    q_dom = f'AUTH:"{name}" AND {dom_clause}'
    q_all = f'AUTH:"{name}"'
    recs, hit_dom, hit_all, err = [], 0, 0, None
    try:
        cursor = "*"
        while len(recs) < max_records:
            p = {"query": q_dom, "format": "json", "pageSize": str(page_size),
                 "resultType": "core", "cursorMark": cursor}
            r = http_json(EPMC + "?" + urllib.parse.urlencode(p))
            hit_dom = r.get("hitCount", 0)
            got = (r.get("resultList") or {}).get("result", [])
            recs += got
            nxt = r.get("nextCursorMark")
            if not got or not nxt or nxt == cursor:
                break
            cursor = nxt
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:80]}"
        FAIL[name] += 1
    try:
        p = {"query": q_all, "format": "json", "pageSize": "1", "resultType": "idlist"}
        hit_all = http_json(EPMC + "?" + urllib.parse.urlencode(p)).get("hitCount", 0)
    except Exception:
        hit_all = 0
    return recs[:max_records], hit_dom, hit_all, err


def to_paper(r):
    """EPMC core record → 与站内语料同构的最小记录(只留打分需要的字段)。"""
    ji = (r.get("journalInfo") or {})
    au = [a.get("fullName") or "" for a in ((r.get("authorList") or {}).get("author") or [])]
    d = (r.get("firstPublicationDate") or r.get("electronicPublicationDate")
         or (str(ji.get("yearOfPublication") or "") + "-01-01"))
    yr = 0
    m = re.match(r"(\d{4})", d or "")
    if m:
        yr = int(m.group(1))
    return {
        "pmid": r.get("pmid") or "", "doi": r.get("doi") or "",
        "title": r.get("title") or "", "abstract": r.get("abstractText") or "",
        "journal": (ji.get("journal") or {}).get("title") or "",
        "year": yr, "date": d or "", "authors": au,
        "affil": r.get("affiliation") or "",
        "is_oa": (r.get("isOpenAccess") == "Y"),
        "cites": int(r.get("citedByCount") or 0),
        "keywords": (r.get("keywordList") or {}).get("keyword", []) or [],
        "mesh": [h.get("descriptorName", "") for h in
                 ((r.get("meshHeadingList") or {}).get("meshHeading") or [])],
        "url": (f"https://europepmc.org/article/MED/{r['pmid']}" if r.get("pmid")
                else (f"https://doi.org/{r['doi']}" if r.get("doi") else "")),
    }


# ---------------------------------------------------------------- 合作者锚定
def distinctive_pool(coauthor_keys, surname_freq):
    """锚定池必须由**本身可区分**的合作者组成。

    第一版直接用全部合作者,结果是共同姓名越常见锚定率越高(Wang Y 58% > Bartel DP 24%)——
    完全反了:因为 'wang|y' 的合作者也是 'li|j'、'zhang|y' 这类高频名,它们到处都能匹配上,
    锚定的是姓名串而不是人。所以这里剔除:
      · 姓氏在语料里出现 ≥6 种不同缩写的(高频姓)
      · 名缩写只有 1 个字母的(区分度不足)
    """
    out = []
    for k in coauthor_keys:
        sur, _, ini = k.partition("|")
        if len(ini) <= 1:
            continue
        if surname_freq.get(sur, 0) >= 6:
            continue
        out.append(k)
    return out


def anchor_records(recs, pi_key, corpus_coauthors):
    """同名消歧的**可计算**手段:一条 EPMC 记录如果与站内语料中同名 PI 的
    已知合作者有交集,它属于同一个人的概率大得多。

    这不是完美消歧(大实验室成员会跳槽、同名者也可能有同名合作者),
    但它是本站唯一能离线验证的证据,所以:
      · anchored=True 的记录进入方向指纹的「高可信」子集
      · anchored=False 的记录仍然保留并显示,但标注为「未锚定」
      · 前端两组分数都给,用户自己判断
    """
    known = set(corpus_coauthors)
    for r in recs:
        keys = set()
        for a in r.get("authors") or []:
            pn = parse_name(a)
            if pn:
                keys.add(name_key(pn[0], pn[1]))
        keys.discard(pi_key)
        shared = sorted(keys & known)
        r["anchor_n"] = len(shared)
        r["anchor_with"] = shared[:6]
        r["anchored"] = len(shared) > 0
    n_anch = sum(1 for r in recs if r["anchored"])
    return recs, n_anch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=60, help="抓取的 PI 数量上限")
    ap.add_argument("--budget", type=int, default=900, help="总墙钟预算(秒),到点写出已抓部分")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-records", type=int, default=200)
    ap.add_argument("--offline", action="store_true", help="只做语料层,不联网")
    a = ap.parse_args()

    cfg = json.load(open(os.path.join(DATA, "interests.json")))
    corpus = json.load(open(os.path.join(DATA, "papers.json")))["papers"]
    cands, simap = build_candidates(corpus, min_last=1)
    global SURFREQ
    SURFREQ = {s: len(v) for s, v in simap.items()}   # 姓氏 → 不同名缩写数

    # 抓取优先级 —— 刻意**不**按末位发文数排。
    # 按发文数排,榜首必然是 Zhang X / Li Y 这类姓名串,而它们在 Europe PMC
    # 里对应几十万篇文章、几百个不同的人,抓回来的"作品集"没有意义。
    # 改为按「可消歧程度」排:先看是否有可用的锚定池(可区分合作者),再看风险等级,
    # 最后才看发文数。抓不动的高风险名字仍会被收录并展示,但不占抓取预算。
    risk_rank = {"low": 0, "medium": 1, "high": 2}
    for c in cands:
        c["_pool"] = len(distinctive_pool([x["key"] for x in c["coauthors"]], SURFREQ))
    # 先按风险分层(可信度优先),层内按末位发文数排(资深度),
    # 锚定池只作为最后的平手裁决 —— 否则会把只发过 1 篇的冷门名字顶上榜首。
    ranked = sorted(cands, key=lambda c: (risk_rank[c["risk"]], -c["n_last"],
                                          -c["n_any"], -c["_pool"]))
    targets = ranked[:a.limit]
    print(f"targets: risk={Counter(c['risk'] for c in targets)} "
          f"median_pool={sorted(c['_pool'] for c in targets)[len(targets)//2]}")

    dom, dom_terms = domain_clause(cfg)
    fetched, errors = {}, {}

    if not a.offline:
        def job(c):
            if time.time() - T0 > a.budget:
                return c["key"], None, "budget_exhausted"
            recs, hd, ha, err = epmc_author(c["display"], dom,
                                            max_records=a.max_records)
            papers_ = [to_paper(r) for r in recs]
            co = distinctive_pool([x["key"] for x in c["coauthors"]], SURFREQ)
            papers_, n_anch = anchor_records(papers_, c["key"], co)
            return c["key"], {"recs": papers_, "hit_domain": hd, "hit_total": ha,
                              "n_anchored": n_anch,
                              "anchor_pool": len(co)}, err

        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            futs = {ex.submit(job, c): c for c in targets}
            done = 0
            for f in as_completed(futs):
                k, payload, err = f.result()
                done += 1
                if payload:
                    fetched[k] = payload
                if err:
                    errors[k] = err
                if done % 10 == 0:
                    print(f"  fetched {done}/{len(targets)}  "
                          f"elapsed {time.time()-T0:.0f}s  errors {len(errors)}", flush=True)
                # 失败率过半 → 中止,写出已抓部分而不是整轮丢失
                if done >= 12 and len(errors) / done > 0.5:
                    print("！失败率 >50%,提前中止,写出已抓部分", flush=True)
                    for g in futs:
                        g.cancel()
                    break

    json.dump({"targets": [c["key"] for c in targets], "fetched": fetched,
               "errors": errors, "dom_terms": dom_terms,
               "elapsed_s": round(time.time() - T0, 1)},
              open(os.path.join(ROOT, "handoff", "pi_fetch.json"), "w"), ensure_ascii=False)
    print(f"targets={len(targets)} fetched={len(fetched)} errors={len(errors)} "
          f"elapsed={time.time()-T0:.0f}s")
    if fetched:
        ns = [len(v["recs"]) for v in fetched.values()]
        print(f"records per PI: min={min(ns)} med={sorted(ns)[len(ns)//2]} max={max(ns)} "
              f"total={sum(ns)}")


if __name__ == "__main__":
    os.makedirs(os.path.join(ROOT, "handoff"), exist_ok=True)
    main()
