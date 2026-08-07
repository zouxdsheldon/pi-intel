#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从站内语料里抽 PI 候选,并给每个姓名打「同名风险」等级。

设计原则(与文献层一致):所有数值都是从 data/papers.json 里可复算的计数或余弦,
不调用任何大模型。同名(homonym)问题**不掩盖**——它是本层最大的已知误差来源,
所以每个候选都带 risk 等级 + 逐条理由,前端必须显示出来。

末位作者 = 通讯作者 是一个**代理**,不是事实:
  - 生物医学多数期刊末位=资深作者,但共同通讯、字母序作者表、合作大文章都会破坏它;
  - 因此我们同时记录 n_last(末位次数)与 n_any(任意位次数),前端两个都显示。
"""
import json, os, re, math, sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")


# ------------------------------------------------------------------ 姓名规范化
# 语料里有两种形态:
#   PubMed/EPMC : "Zhang X"      (姓 + 首字母缩写)
#   bioRxiv     : "Zhang, X. Y." (姓, 名首字母带点)
# 统一成 ("zhang", "xy") 的 (surname, initials) 二元组,便于跨源合并。
_PUNCT = re.compile(r"[.\u2019']")


def parse_name(raw):
    """→ (surname_key, initials_key, display) ;无法解析时 initials_key=''。"""
    s = (raw or "").strip()
    if not s:
        return None
    if "," in s:                                   # bioRxiv 形态
        sur, rest = s.split(",", 1)
        sur = sur.strip()
        ini = _PUNCT.sub("", rest).replace(" ", "")
    else:                                          # PubMed 形态
        parts = s.split()
        if len(parts) == 1:
            sur, ini = parts[0], ""
        else:
            # 末段若全大写且短 → 是缩写;否则是全名(如 "Eric Lai" 反序少见,按末段为姓)
            tail = parts[-1]
            if tail.isupper() and len(tail) <= 3:
                sur, ini = " ".join(parts[:-1]), tail
            else:
                sur, ini = tail, "".join(w[0] for w in parts[:-1])
    sur_k = re.sub(r"[^a-z\-]", "", sur.lower())
    ini_k = re.sub(r"[^a-z]", "", (ini or "").lower())
    if not sur_k:
        return None
    disp = (sur + " " + ini.upper()).strip() if ini else sur
    return sur_k, ini_k, disp


def name_key(sur_k, ini_k):
    return sur_k + "|" + ini_k


# ------------------------------------------------------------------ 单位串
# EPMC 的 affiliation 是**第一作者**单位(不是每作者单位),所以对末位作者只是弱信号。
# 我们只抽到机构级别,不试图抽系/楼层——那样的精度这份数据给不了。
# 单位串是逗号分隔的,所以按逗号切段比在整串上跑正则可靠得多:
# 正则容易把街道地址 "930 N. University Avenue" 抓成 "N. University"。
INST_KEY = re.compile(
    r"\b(University|Universit[eyà]|Universidad|Universität|Universiteit|Institute|Institut|"
    r"College|Hospital|Center|Centre|School of|Academy|Laboratory|Laboratoire|"
    r"Clinic|Medical Cent|Cancer Cent)\b", re.I)
# 街道/邮编特征:含门牌号、Ave/St/Rd/Blvd、邮编 → 是地址不是机构名
ADDR_PAT = re.compile(
    r"(^\s*\d|\b\d{3,}\b|\b(Avenue|Ave|Street|St\.|Road|Rd|Boulevard|Blvd|Drive|Dr\.|"
    r"Suite|Floor|Box|Building|Bldg)\b)", re.I)

REGION_MAP = [
    ("🇺🇸 美国", ["usa", "united states", ", ny", ", ma", ", ca", ", tx", ", md", "bethesda",
                "new york", "boston", "cambridge, ma", "stanford", "harvard", "mit ",
                "sloan kettering", "nih", "california", "seattle", "chicago", "houston"]),
    ("🇨🇳 中国大陆", ["china", "beijing", "shanghai", "guangzhou", "wuhan", "hangzhou", "nanjing",
                  "chengdu", "xi'an", "tsinghua", "peking", "fudan", "zhejiang", "sichuan"]),
    ("🇭🇰 香港", ["hong kong", "hksar"]),
    ("🇸🇬 新加坡", ["singapore"]),
    ("🇯🇵 日本", ["japan", "tokyo", "kyoto", "osaka"]),
    ("🇰🇷 韩国", ["korea", "seoul", "kaist"]),
    ("🇬🇧 英国", ["united kingdom", " uk", "england", "london", "oxford", "cambridge, uk",
                "scotland", "edinburgh", "manchester"]),
    ("🇩🇪 德国", ["germany", "deutschland", "berlin", "munich", "münchen", "heidelberg", "max planck"]),
    ("🇫🇷 法国", ["france", "paris", "lyon", "inserm", "cnrs"]),
    ("🇨🇦 加拿大", ["canada", "toronto", "montreal", "vancouver"]),
    ("🇦🇺 澳洲", ["australia", "melbourne", "sydney"]),
    ("🇨🇭 瑞士", ["switzerland", "zurich", "basel", "lausanne", "epfl"]),
    ("🇳🇱 荷兰", ["netherlands", "amsterdam", "utrecht", "leiden"]),
    ("🇮🇱 以色列", ["israel", "weizmann", "technion"]),
    ("🇪🇸 西班牙", ["spain", "barcelona", "madrid"]),
    ("🇮🇹 意大利", ["italy", "milan", "rome"]),
    ("🇸🇪 瑞典", ["sweden", "karolinska", "stockholm"]),
    ("🇮🇳 印度", ["india", "bangalore", "delhi"]),
]


def guess_region(affils):
    blob = " ".join(affils).lower()
    for name, keys in REGION_MAP:
        if any(k in blob for k in keys):
            return name
    return ""


    # 系/科室级别的段落丢掉——这份数据不足以可靠区分同机构的不同系
DEPT_PAT = re.compile(r"^\s*(Department|Dept|Division|Program|Programme|Faculty|Section|"
                      r"Unit|Group|Lab\b|Key Laboratory|State Key)", re.I)


def extract_inst(affil):
    """按逗号切段,保留含机构关键词、且不像地址/科室的段。"""
    if not affil:
        return []
    out, seen = [], set()
    for seg in re.split(r"[,;]", affil):
        t = re.sub(r"\s+", " ", seg).strip(" .;")
        # 段首残留的国名/州名(逗号切开后常见,如 "USA UF Health Cancer Center")
        t = re.sub(r"^(USA|UK|China|Japan|Germany|France|Canada|Australia|India|Italy|Spain)\s+",
                   "", t, flags=re.I).strip()
        # 上限放宽到 95:NIH 那类全称("National Institute of Diabetes and Digestive
        # and Kidney Diseases")本身就有 70+ 字符,砍到 70 会把真机构整段丢掉
        if not (6 <= len(t) <= 95):
            continue
        if not INST_KEY.search(t):
            continue
        if ADDR_PAT.search(t) or DEPT_PAT.match(t):
            continue
        # 段尾是 "University of" 这类被逗号截断的,补上下一段(如 "University of California, San Diego")
        if t.lower().endswith((" of", " for", " and")):
            continue
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


# ------------------------------------------------------------------ 同名风险
def homonym_risk(cand, surname_initial_map, corpus_n):
    """返回 (level, score, reasons[]) —— level ∈ low/medium/high。

    四项证据,任何一项都可单独把风险推高:
      1. 同姓不同缩写的数量:语料里 "Zhang" 后面跟着 11 种不同缩写 → 这个姓在本领域极常见
      2. 名字形态:只有 1 个首字母缩写(如 "Li Y")比 "McJunkin K" 或全名歧义大得多
      3. 观察到的单位数:同一姓名串出现在 4 个互不相干的机构 → 大概率不是同一个人
      4. 方向向量离散度:同一姓名串的论文彼此主题毫不相干 → 也是混人的迹象
    """
    reasons, score = [], 0.0
    sur, ini = cand["surname"], cand["initials"]

    n_sib = len(surname_initial_map.get(sur, ()))
    if n_sib >= 8:
        score += 2.0
        reasons.append(f"「{sur.title()}」是语料中的高频姓氏(共出现 {n_sib} 种不同名缩写)")
    elif n_sib >= 4:
        score += 1.0
        reasons.append(f"「{sur.title()}」在语料中有 {n_sib} 种不同名缩写,同姓者多")

    if len(ini) <= 1:
        score += 1.5
        reasons.append(f"名字只有 {len(ini)} 个首字母,区分度低" if ini else "只有姓、无名缩写")
    elif len(ini) == 2:
        score += 0.4

    n_inst = len(cand["institutions"])
    if n_inst >= 4:
        score += 1.5
        reasons.append(f"同一姓名串出现在 {n_inst} 个不同机构")
    elif n_inst == 3:
        score += 0.7
        reasons.append("同一姓名串出现在 3 个不同机构")

    disp = cand.get("dir_dispersion", 0.0)
    if disp >= 0.75 and cand["n_last"] >= 3:
        score += 1.2
        reasons.append(f"名下论文主题高度离散(方向离散度 {disp:.2f}),疑似多人合并")
    elif disp >= 0.6 and cand["n_last"] >= 3:
        score += 0.5

    level = "high" if score >= 3.0 else ("medium" if score >= 1.5 else "low")
    if level == "low" and not reasons:
        reasons.append("姓氏不高频、名缩写有区分度、单位一致 —— 但仍不能排除同名")
    return level, round(score, 2), reasons


# ------------------------------------------------------------------ 主流程
def build_candidates(papers, min_last=1):
    by = {}
    surname_initial_map = defaultdict(set)

    for p in papers:
        au = p.get("authors") or []
        if not au:
            continue
        parsed = [parse_name(a) for a in au]
        parsed = [x for x in parsed if x]
        if not parsed:
            continue
        for sur_k, ini_k, _ in parsed:
            surname_initial_map[sur_k].add(ini_k)

        for pos, (sur_k, ini_k, disp) in enumerate(parsed):
            k = name_key(sur_k, ini_k)
            c = by.setdefault(k, {
                "key": k, "surname": sur_k, "initials": ini_k, "display": disp,
                "n_last": 0, "n_first": 0, "n_any": 0,
                "papers_last": [], "papers_any": [],
                "institutions": Counter(), "affils": [],
                "journals": Counter(), "tiers": Counter(),
                "years": Counter(), "coauthors": Counter(),
            })
            c["n_any"] += 1
            c["papers_any"].append(p["i"])
            is_last = (pos == len(parsed) - 1) and len(parsed) > 1
            is_first = (pos == 0)
            if is_last:
                c["n_last"] += 1
                c["papers_last"].append(p["i"])
                # 单位串只在末位时采信度稍高;但 EPMC 给的是首作者单位,故记为"观察到"而非"所属"
                for inst in extract_inst(p.get("affil")):
                    c["institutions"][inst] += 1
                if p.get("affil"):
                    c["affils"].append(p["affil"])
            if is_first:
                c["n_first"] += 1
            if p.get("journal"):
                c["journals"][p["journal"]] += 1
            if p.get("tier"):
                c["tiers"][p["tier"]] += 1
            if p.get("year"):
                c["years"][p["year"]] += 1
            for other in parsed:
                ok = name_key(other[0], other[1])
                if ok != k:
                    c["coauthors"][ok] += 1

    # 方向离散度:同一姓名下所有论文的 dirs 向量两两余弦的 1-mean
    pidx = {p["i"]: p for p in papers}
    for c in by.values():
        vecs = []
        for i in c["papers_any"]:
            d = (pidx[i].get("dirs") or {})
            if d:
                vecs.append({k2: v["score"] for k2, v in d.items()})
        c["dir_dispersion"] = round(_dispersion(vecs), 4)

    out = [c for c in by.values() if c["n_last"] >= min_last]
    for c in out:
        lvl, sc, rs = homonym_risk(c, surname_initial_map, len(papers))
        c["risk"] = lvl
        c["risk_score"] = sc
        c["risk_reasons"] = rs
        c["region"] = guess_region(c["affils"])
        c["institutions"] = [{"name": n, "n": k} for n, k in c["institutions"].most_common(5)]
        c["journals"] = [{"name": n, "n": k} for n, k in c["journals"].most_common(8)]
        c["tiers"] = dict(c["tiers"])
        c["years"] = {str(y): n for y, n in sorted(c["years"].items())}
        c["coauthors"] = [{"key": n, "n": k} for n, k in c["coauthors"].most_common(40)]
        c.pop("affils", None)
    out.sort(key=lambda c: (-c["n_last"], -c["n_any"]))
    return out, surname_initial_map


def _dispersion(vecs):
    """1 - 平均两两余弦。空/单篇 → 0(无证据即不加分)。"""
    if len(vecs) < 2:
        return 0.0
    keys = sorted({k for v in vecs for k in v})
    arr = []
    for v in vecs:
        x = [v.get(k, 0.0) for k in keys]
        n = math.sqrt(sum(t * t for t in x)) or 1.0
        arr.append([t / n for t in x])
    tot, cnt = 0.0, 0
    for a in range(len(arr)):
        for b in range(a + 1, len(arr)):
            tot += sum(p * q for p, q in zip(arr[a], arr[b]))
            cnt += 1
    return max(0.0, 1.0 - (tot / cnt if cnt else 0.0))


def main():
    papers = json.load(open(os.path.join(DATA, "papers.json")))["papers"]
    cands, simap = build_candidates(papers, min_last=1)
    os.makedirs(os.path.join(ROOT, "handoff"), exist_ok=True)
    json.dump({"candidates": cands, "corpus_n": len(papers)},
              open(os.path.join(ROOT, "handoff", "pi_seed.json"), "w"),
              ensure_ascii=False)
    rc = Counter(c["risk"] for c in cands)
    print(f"corpus={len(papers)}  candidates={len(cands)}  risk={dict(rc)}")
    print("n_last>=3:", sum(1 for c in cands if c["n_last"] >= 3),
          "| n_last>=2:", sum(1 for c in cands if c["n_last"] >= 2))
    for c in cands[:12]:
        print(f"  {c['display']:<16} last={c['n_last']:<3} any={c['n_any']:<3} "
              f"risk={c['risk']:<6} region={c['region'] or '—':<10} "
              f"inst={(c['institutions'][0]['name'][:34] if c['institutions'] else '—')}")


if __name__ == "__main__":
    main()
