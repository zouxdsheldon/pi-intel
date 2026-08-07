#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""方向指纹 / 年度轨迹 / 竞争度 · 互补度 · 合作可能 → data/pis.json

复用 fetch_papers.py 的 score_papers() 与 journal_tier():PI 的方向分与站内
文献卡片的分数**同尺度**,可以交叉核验(同一篇文章在两处应给出同一个分)。

所有指标都写明分子分母,前端可展开看构成 —— 不给单一「契合度」黑箱分。
"""
import argparse, json, math, os, re, sys, time
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
sys.path.insert(0, HERE)

from _scoring import score_papers, journal_tier, tokens          # noqa: E402
from pi_extract import build_candidates                           # noqa: E402
from _methods import extract_methods                              # noqa: E402


def method_terms(p):
    """只取词表命中的规范词,保留其所属方法大类(测序/遗传操作/生化/成像/模型/计算)。"""
    return [(m["term"], m["group"]) for m in
            extract_methods(p.get("title", ""), p.get("abstract", ""),
                            p.get("keywords"), p.get("mesh"))]

HALF_LIFE_DAYS = 540.0        # 与文献层的新颖度半衰期一致
NOW_YEAR = time.gmtime().tm_year


def recency_w(year):
    """近期加权:与文献层同一半衰期,保证「近 5 年方向」不被十年前的旧作淹没。"""
    if not year:
        return 0.2
    days = max(0, (NOW_YEAR - year)) * 365.25
    return 0.5 ** (days / HALF_LIFE_DAYS)


# ---------------------------------------------------------------- 指纹
def fingerprint(scored, int_ids, subset=None):
    """→ {dir_id: 加权平均分}, 加权用近期权重。subset=None 用全部记录。"""
    recs = scored if subset is None else [r for r in scored if subset(r)]
    acc = {k: 0.0 for k in int_ids}
    wsum = 0.0
    for r in recs:
        w = recency_w(r.get("year"))
        wsum += w
        for k, v in (r.get("dirs") or {}).items():
            if k in acc:
                acc[k] += w * v["score"]
    if wsum <= 0:
        return {k: 0.0 for k in int_ids}, 0
    return {k: round(v / wsum, 4) for k, v in acc.items()}, len(recs)


def trajectory(scored, int_ids, years_back=8):
    """年度方向占比矩阵 —— 看得出某 PI 是否正在向 TDMD/代谢方向漂移。"""
    by_year = defaultdict(lambda: {k: 0.0 for k in int_ids})
    cnt = Counter()
    for r in scored:
        y = r.get("year") or 0
        if y < NOW_YEAR - years_back or y > NOW_YEAR + 1:
            continue
        cnt[y] += 1
        for k, v in (r.get("dirs") or {}).items():
            if k in int_ids:
                by_year[y][k] += v["score"]
    out = []
    for y in sorted(by_year):
        tot = sum(by_year[y].values()) or 1.0
        out.append({"year": y, "n": cnt[y],
                    "share": {k: round(v / tot, 4) for k, v in by_year[y].items() if v > 0}})
    return out


def drift(scored, int_ids, split=3):
    """方向漂移量 = 近 split 年向量 与 更早 split 年向量 的夹角(度)。
    样本不足(任一侧 <3 篇)时返回 None —— 不拿 1 篇文章算趋势。"""
    recent = [r for r in scored if (r.get("year") or 0) >= NOW_YEAR - split]
    older = [r for r in scored if NOW_YEAR - 2 * split <= (r.get("year") or 0) < NOW_YEAR - split]
    if len(recent) < 3 or len(older) < 3:
        return {"deg": None, "n_recent": len(recent), "n_older": len(older),
                "note": "样本不足(任一侧 <3 篇),不计算漂移"}
    a, _ = fingerprint(recent, int_ids)
    b, _ = fingerprint(older, int_ids)
    cos = _cos(a, b)
    return {"deg": round(math.degrees(math.acos(max(-1.0, min(1.0, cos)))), 1),
            "n_recent": len(recent), "n_older": len(older),
            "recent": a, "older": b,
            "gained": sorted(((k, round(a[k] - b[k], 4)) for k in int_ids),
                             key=lambda kv: -kv[1])[:3],
            "lost": sorted(((k, round(a[k] - b[k], 4)) for k in int_ids),
                           key=lambda kv: kv[1])[:3]}


def _cos(a, b):
    keys = set(a) | set(b)
    na = math.sqrt(sum(a.get(k, 0) ** 2 for k in keys)) or 1.0
    nb = math.sqrt(sum(b.get(k, 0) ** 2 for k in keys)) or 1.0
    return sum(a.get(k, 0) * b.get(k, 0) for k in keys) / (na * nb)


# ---------------------------------------------------------------- 三项指标
def my_vector(cfg):
    """「我的方向」向量 = interests.json 里的权重本身(它就是用户申报的方向优先级)。"""
    return {it["id"]: float(it["w"]) for it in cfg["interests"]}


def competition(fp, mine):
    """竞争度 = PI 指纹 与 我的方向权重向量 的余弦。
    高 = 做的东西和我高度重叠 → 正面对手(也可能是最合适的推荐人/审稿人)。"""
    c = _cos(fp, mine)
    top = sorted(((k, round(fp.get(k, 0) * mine.get(k, 0), 4)) for k in mine),
                 key=lambda kv: -kv[1])[:4]
    return round(c, 4), top


MIN_METHODS_FOR_COMPLEMENT = 4    # 低于此数不给互补度分,只给 None


def rarity_cut(corpus_method_freq, corpus_n):
    """稀缺阈值取语料方法词表 share 的**中位数**,而不是拍脑袋的固定 5%。

    实测:语料 1093 篇里 110 个方法词,share 中位数仅 0.0037,97% 的词都 <0.05。
    用 5% 当阈值等于「几乎所有方法都算稀缺」,互补度会集体顶到 0.9+,失去区分力。
    改成相对阈值后,「稀缺」的定义变成「比这个语料里一半的方法更少见」。
    """
    shares = sorted(v / max(1, corpus_n) for v in corpus_method_freq.values())
    return shares[len(shares) // 2] if shares else 0.0


def complement(pi_methods, corpus_method_freq, corpus_n, cut):
    """互补度 = PI 掌握、而站内语料稀缺的方法占比(分子/分母都上屏)。

    样本护栏:检出方法 <4 个时返回 None 而不是 1.0。
    只检出 1 个方法且它恰好稀缺 → 1/1 = 满分,那是**没测到**,不是「高度互补」;
    前端必须显示「方法样本不足」而不是一个漂亮的满分。
    """
    if len(pi_methods) < MIN_METHODS_FOR_COMPLEMENT:
        return None, [], {"n_methods": len(pi_methods),
                          "note": f"检出方法仅 {len(pi_methods)} 个(<{MIN_METHODS_FOR_COMPLEMENT}),不计互补度"}
    rare = []
    for m, n in pi_methods.items():
        share = corpus_method_freq.get(m, 0) / max(1, corpus_n)
        if share < cut:
            rare.append({"method": m, "pi_n": n, "corpus_share": round(share, 4)})
    rare.sort(key=lambda x: (x["corpus_share"], -x["pi_n"]))
    return (round(len(rare) / len(pi_methods), 4), rare[:8],
            {"n_methods": len(pi_methods), "n_rare": len(rare), "cut": round(cut, 4)})


def collab_score(comp, compl_):
    if compl_ is None:          # 互补度未测到 → 合作可能也不给分,不用 0 冒充
        return None
    """合作可能 = 互补度高、但正面竞争不过头。
    刻意用乘性惩罚而非加权和:竞争度极高时(>0.85)哪怕互补度满分也压下去,
    因为那种情况现实里是抢课题,不是合作。"""
    penalty = 1.0 if comp < 0.55 else max(0.0, 1.0 - (comp - 0.55) / 0.35)
    return round(compl_ * penalty, 4)


def head_on(scored, flagship_terms, top_n=6):
    """与旗舰方向(ZSWIM8-6:AMPK×TDMD×代谢记忆)的正面撞车清单。"""
    hits = []
    for r in scored:
        blob = ((r.get("title") or "") + " " + (r.get("abstract") or "")).lower()
        matched = [t for t in flagship_terms if t in blob]
        if len(matched) >= 2:
            hits.append({"title": r.get("title", "")[:160], "year": r.get("year"),
                         "journal": r.get("journal", ""), "url": r.get("url", ""),
                         "pmid": r.get("pmid", ""), "terms": matched[:6],
                         "anchored": bool(r.get("anchored"))})
    hits.sort(key=lambda h: (-(h["year"] or 0), -len(h["terms"])))
    return hits[:top_n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-records", type=int, default=3,
                    help="作品集少于这么多篇的 PI 不计算指纹(样本太小)")
    a = ap.parse_args()

    cfg = json.load(open(os.path.join(DATA, "interests.json")))
    corpus = json.load(open(os.path.join(DATA, "papers.json")))["papers"]
    fetch = json.load(open(os.path.join(ROOT, "handoff", "pi_fetch.json")))
    cands, _ = build_candidates(corpus, min_last=1)
    by_key = {c["key"]: c for c in cands}

    int_ids = [it["id"] for it in cfg["interests"]]
    mine = my_vector(cfg)

    # 站内语料的方法词频(互补度的分母基准)
    corpus_mf, mgroup = Counter(), {}
    for p in corpus:
        for t, g in method_terms(p):
            corpus_mf[t] += 1
            mgroup[t] = g
    CUT = rarity_cut(corpus_mf, len(corpus))

    flagship_terms = ["ampk", "zswim8", "tdmd", "target-directed", "metabolic memory",
                      "energy stress", "mir-29", "prkaa", "ampk activation"]

    out = []
    for key, payload in fetch["fetched"].items():
        c = by_key.get(key)
        if not c:
            continue
        recs = payload["recs"]
        if len(recs) < a.min_records:
            continue
        # 用站内同一个打分器打分 —— 保证同尺度
        for r in recs:
            r.setdefault("abstract", "")
            r.setdefault("mesh", [])
            r.setdefault("keywords", [])
        score_papers(recs, cfg)
        for r in recs:
            r["tier"] = journal_tier(r.get("journal"))

        fp_all, n_all = fingerprint(recs, int_ids)
        fp_anch, n_anch = fingerprint(recs, int_ids, subset=lambda r: r.get("anchored"))
        traj = trajectory(recs, int_ids)
        dr = drift(recs, int_ids)

        pi_methods = Counter()
        for r in recs:
            for t, g in method_terms(r):
                pi_methods[t] += 1
                mgroup.setdefault(t, g)

        comp, comp_top = competition(fp_all, mine)
        compl_, rare, compl_parts = complement(pi_methods, corpus_mf, len(corpus), CUT)
        collab = collab_score(comp, compl_)

        tiers = Counter(r["tier"] for r in recs if r.get("tier"))
        top_papers = sorted(recs, key=lambda r: -(r.get("score") or 0))[:12]

        out.append({
            "key": key, "display": c["display"],
            "risk": c["risk"], "risk_score": c["risk_score"], "risk_reasons": c["risk_reasons"],
            "region": c["region"], "institutions": c["institutions"],
            "corpus_n_last": c["n_last"], "corpus_n_any": c["n_any"],
            "epmc_hit_domain": payload["hit_domain"], "epmc_hit_total": payload["hit_total"],
            "n_records": len(recs), "n_anchored": payload["n_anchored"],
            "anchor_pool": payload["anchor_pool"],
            "fp": fp_all, "fp_anchored": fp_anch, "n_fp": n_all, "n_fp_anchored": n_anch,
            "trajectory": traj, "drift": dr,
            "competition": comp, "competition_top": comp_top,
            "complement": compl_, "rare_methods": rare, "complement_parts": compl_parts,
            "collab": collab,
            "methods": [{"m": m, "n": n, "g": mgroup.get(m, ""),
                         "corpus_share": round(corpus_mf.get(m, 0) / max(1, len(corpus)), 4)}
                        for m, n in pi_methods.most_common(16)],
            "tiers": dict(tiers), "oa_share": round(
                sum(1 for r in recs if r.get("is_oa")) / max(1, len(recs)), 3),
            "head_on": head_on(recs, flagship_terms),
            "top_papers": [{"title": r["title"][:180], "year": r["year"],
                            "journal": r["journal"], "tier": r.get("tier"),
                            "score": r.get("score"), "url": r.get("url"),
                            "pmid": r.get("pmid"), "cites": r.get("cites", 0),
                            "anchored": bool(r.get("anchored")),
                            "top_dir": max((r.get("dirs") or {}).items(),
                                           key=lambda kv: kv[1]["score"],
                                           default=(None, {}))[0],
                            "hits": sorted({h for v in (r.get("dirs") or {}).values()
                                            for h in (v.get("core", []) + v.get("peri", []))})[:8]}
                           for r in top_papers],
            "years": c["years"],
        })

    out.sort(key=lambda x: -x["competition"])

    # ---- 被排除的候选:必须上屏,否则"同名问题"就被藏起来了 ----
    # 抓取只针对低风险名字。如果榜单只显示抓到的 48 个人,读者会以为语料里就这些 PI,
    # 而实际上发文最多的那批名字(Zhang X / Li Y …)恰恰因为无法消歧才不在榜上。
    # 这里把它们连同"为什么被排除"一起输出,前端灰显。
    fetched_keys = set(fetch["fetched"])
    excluded = []
    for c in sorted(cands, key=lambda c: -c["n_last"]):
        if c["key"] in fetched_keys or c["n_last"] < 2:
            continue
        excluded.append({"display": c["display"], "risk": c["risk"],
                         "risk_reasons": c["risk_reasons"],
                         "n_last": c["n_last"], "n_any": c["n_any"],
                         "region": c["region"],
                         "why": ("同名风险过高,姓名串对应多个不同的人,"
                                 "作品集无法归属到个人" if c["risk"] != "low"
                                 else "低风险但未进入本轮抓取预算")})
        if len(excluded) >= 60:
            break
    meta = {
        "updated": time.strftime("%Y-%m-%d", time.gmtime()),
        "corpus_n": len(corpus), "n_pis": len(out),
        "int_ids": int_ids,
        "int_names": {it["id"]: it["name"] for it in cfg["interests"]},
        "int_colors": {it["id"]: it.get("color", "#888") for it in cfg["interests"]},
        "my_vector": mine,
        "dom_terms": fetch.get("dom_terms", []),
        "fetch_errors": len(fetch.get("errors", {})),
        "fetch_targets": len(fetch.get("targets", [])),
        "flagship_terms": flagship_terms,
        "half_life_days": HALF_LIFE_DAYS,
        "rarity_cut": round(CUT, 5),
        "min_methods_for_complement": MIN_METHODS_FOR_COMPLEMENT,
        "n_candidates": len(cands),
        "n_excluded_shown": None,   # 下面填
        "risk_dist": dict(Counter(c["risk"] for c in cands)),
        "source": "Europe PMC REST (免 key);方向分用站内 interests.json 同一套权重",
    }
    meta["n_excluded_shown"] = len(excluded)
    json.dump({"meta": meta, "pis": out, "excluded": excluded},
              open(os.path.join(DATA, "pis.json"), "w"), ensure_ascii=False)
    print(f"excluded shown: {len(excluded)}  candidates: {len(cands)}")
    print(f"PIs written: {len(out)}  (targets={meta['fetch_targets']}, "
          f"errors={meta['fetch_errors']})")
    print("competition top5:",
          [(p["display"], p["competition"], p["risk"]) for p in out[:5]])
    scored_ = [p for p in out if p["collab"] is not None]
    print(f"collab measurable: {len(scored_)}/{len(out)}  cut={CUT:.4f}")
    print("collab top5:", [(p["display"], p["collab"], p["complement_parts"]["n_methods"])
                           for p in sorted(scored_, key=lambda x: -x["collab"])[:5]])


if __name__ == "__main__":
    main()
