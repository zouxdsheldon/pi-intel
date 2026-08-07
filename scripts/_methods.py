#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_papers.py — 把「找到文献」升级为「读懂文献」的离线分析层。

设计底线:**不用 LLM,不编造**。每一个输出字段都能被读者当场核对:
  · 摘要分段    -> 若原文自带 BACKGROUND/METHODS/... 标签则直接解析(authoritative);
                   否则用线索词 + 句序做启发式切分,并标 inferred=true。
  · 方法抽取    -> 词典命中,同时返回命中的那一句原文,可点开核对。
  · 语句库      -> 按修辞角色(gap/aim/method/result/significance/limitation)分类的**原文句子**,
                   不改写、不生成。
  · 新颖度      -> 三个可核算的分量:与语料中更早文献的最大相似度、稀有词占比、
                   新术语(近 N 天才首次出现在语料里)数量。给出分量值而非黑箱分数。

被 fetch_papers.py 调用;也可独立运行,对现有 data/papers.json 就地重算:
    python scripts/analyze_papers.py
"""
import os, re, json, math, datetime
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")

# ---------------------------------------------------------------- 摘要分段
SEC_LABEL = re.compile(
    r'\b(BACKGROUND|INTRODUCTION|OBJECTIVES?|AIMS?|PURPOSE|RATIONALE|'
    r'METHODS?|MATERIALS AND METHODS|DESIGN|APPROACH|'
    r'RESULTS?|FINDINGS|'
    r'CONCLUSIONS?|DISCUSSION|SIGNIFICANCE|IMPLICATIONS?|INTERPRETATION)\s*[:：]', re.I)

SEC_MAP = {
    "background": "背景", "introduction": "背景", "rationale": "背景",
    "objective": "目的", "objectives": "目的", "aim": "目的", "aims": "目的",
    "purpose": "目的",
    "method": "方法", "methods": "方法", "materials and methods": "方法",
    "design": "方法", "approach": "方法",
    "result": "结果", "results": "结果", "finding": "结果", "findings": "结果",
    "conclusion": "结论", "conclusions": "结论", "discussion": "结论",
    "significance": "意义", "implication": "意义", "implications": "意义",
    "interpretation": "结论",
}

CUE_BG = re.compile(r'\b(is a|are a|plays? an? |has been (shown|implicated)|it is (well )?known|'
                    r'previous(ly)? (studies|work)|remains? (unclear|unknown|elusive)|little is known|'
                    r'however|although|despite|importantly, )', re.I)
CUE_ME = re.compile(r'\b(we (used|performed|generated|developed|applied|combined|carried out|employed)|'
                    r'here we (use|apply|develop|present|report|combine)|using |by (using|combining|means of)|'
                    r'were (measured|analy[sz]ed|assessed|quantified|treated|transfected|injected)|'
                    r'(mice|cells|samples|patients|organoids) were|to (test|determine|assess|address) (this|whether))', re.I)
CUE_RE = re.compile(r'\b(we (found|show|observed|identified|demonstrate|detected|reveal)|'
                    r'(showed|revealed|indicated|demonstrated) that|'
                    r'(increased|decreased|reduced|elevated|abolished|impaired|enhanced|restored)|'
                    r'significant(ly)?|(p|P)\s*[<=]\s*0?\.\d|fold[- ]change|compared (with|to) (control|wild)|'
                    r'consistent with)', re.I)
CUE_CO = re.compile(r'\b(these (results|data|findings)|our (results|data|findings)|'
                    r'(collectively|together|overall|thus|therefore|in summary|in conclusion)|'
                    r'suggests? that|indicates? that|provides? (a|new|the first)|'
                    r'(may|could|might) (represent|serve|offer|provide)|highlight(s|ing)?|'
                    r'we propose)', re.I)


def split_sentences(t):
    t = re.sub(r'\s+', ' ', t or "").strip()
    if not t:
        return []
    parts = re.split(r'(?<=[.!?])\s+(?=[A-Z(\[])', t)
    return [s.strip() for s in parts if len(s.strip()) > 15]


def segment_abstract(abstract):
    """返回 {'sections': [{'name','zh','text','inferred'}], 'labeled': bool}"""
    a = re.sub(r'\s+', ' ', abstract or "").strip()
    if not a:
        return {"sections": [], "labeled": False}

    # (1) 原文自带小标题 —— 直接解析,权威
    marks = list(SEC_LABEL.finditer(a))
    if len(marks) >= 2:
        secs = []
        for k, m in enumerate(marks):
            raw = m.group(1).strip().lower()
            end = marks[k + 1].start() if k + 1 < len(marks) else len(a)
            body = a[m.end():end].strip(" :：")
            if len(body) < 10:
                continue
            secs.append({"name": raw.title(), "zh": SEC_MAP.get(raw, raw.title()),
                         "text": body, "inferred": False})
        if secs:
            return {"sections": secs, "labeled": True}

    # (2) 无标题 —— 线索词 + 句序启发式;每句独立判定,再合并同类相邻句
    sents = split_sentences(a)
    if not sents:
        return {"sections": [], "labeled": False}
    n = len(sents)
    lab = []
    for i, s in enumerate(sents):
        pos = i / max(1, n - 1)          # 0=开头 1=结尾
        sc = {"背景": 0.0, "方法": 0.0, "结果": 0.0, "结论": 0.0}
        if CUE_BG.search(s): sc["背景"] += 1.0
        if CUE_ME.search(s): sc["方法"] += 1.0
        if CUE_RE.search(s): sc["结果"] += 1.0
        if CUE_CO.search(s): sc["结论"] += 1.0
        # 位置先验(弱):开头偏背景,中段偏方法/结果,结尾偏结论
        sc["背景"] += 0.6 * max(0.0, 1 - pos * 2.5)
        sc["方法"] += 0.35 * (1 - abs(pos - 0.32) * 2.2)
        sc["结果"] += 0.45 * (1 - abs(pos - 0.62) * 2.0)
        sc["结论"] += 0.6 * max(0.0, (pos - 0.6) * 2.5)
        lab.append(max(sc.items(), key=lambda kv: kv[1])[0])

    secs, cur, buf = [], lab[0], [sents[0]]
    for s, l in zip(sents[1:], lab[1:]):
        if l == cur:
            buf.append(s)
        else:
            secs.append({"name": cur, "zh": cur, "text": " ".join(buf), "inferred": True})
            cur, buf = l, [s]
    secs.append({"name": cur, "zh": cur, "text": " ".join(buf), "inferred": True})
    return {"sections": secs, "labeled": False}


# ---------------------------------------------------------------- 方法/技术抽取
METHODS = {
    "测序 / 组学": [
        "rna-seq", "rna sequencing", "small rna-seq", "small rna sequencing", "scrna-seq",
        "single-cell rna", "single cell rna", "snrna-seq", "atac-seq", "chip-seq", "cut&run",
        "cut&tag", "ribo-seq", "ribosome profiling", "clip-seq", "iclip", "eclip", "par-clip",
        "hits-clip", "ago-clip", "clash", "degradome", "gro-seq", "pro-seq", "slam-seq",
        "timelapse-seq", "nanopore", "pacbio", "long-read sequencing", "whole-genome sequencing",
        "whole-exome", "bisulfite", "hi-c", "spatial transcriptomic", "proteomic", "phosphoproteomic",
        "metabolomic", "lipidomic", "mass spectrometry", "lc-ms", "tmt labeling", "silac",
    ],
    "遗传操作": [
        "crispr", "crispr-cas9", "cas9", "cas12", "cas13", "base editing", "base editor",
        "prime editing", "knockout", "knock-in", "knockin", "conditional knockout", "cre-lox",
        "cre/lox", "sirna", "shrna", "antisense oligonucleotide", "aso", "morpholino",
        "lentivirus", "aav", "adeno-associated", "transgenic", "degron", "auxin-inducible",
        "dtag", "overexpression", "rescue experiment", "point mutant", "phosphomimetic",
        "phospho-dead", "s608a", "s608d",
    ],
    "生化 / 结构": [
        "cryo-em", "cryoem", "crystallograph", "x-ray structure", "nmr", "alphafold",
        "co-immunoprecipitation", "co-ip", "immunoprecipitation", "pull-down", "pulldown",
        "in vitro reconstitution", "reconstituted", "emsa", "gel shift", "kinase assay",
        "in vitro kinase", "ubiquitination assay", "deubiquitination", "surface plasmon",
        "spr", "itc", "biolayer interferometry", "proximity labeling", "bioid",
        "turboid", "apex2", "crosslinking mass spectrometry", "size-exclusion", "sec-mals",
        "western blot", "immunoblot", "qpcr", "rt-qpcr", "droplet digital pcr", "ddpcr",
        "northern blot", "luciferase reporter", "reporter assay", "flow cytometry", "facs",
    ],
    "成像": [
        "confocal", "super-resolution", "sted", "storm", "palm", "live-cell imaging",
        "single-molecule", "smfish", "rna fish", "in situ hybridization", "immunofluorescence",
        "immunohistochemistry", "electron microscopy", "two-photon", "light-sheet",
    ],
    "模型体系": [
        "organoid", "enteroid", "ipsc", "induced pluripotent", "primary culture", "co-culture",
        "mouse model", "knockout mice", "germline", "zebrafish", "drosophila", "c. elegans",
        "caenorhabditis", "pig model", "porcine", "xenograft", "pdx", "patient-derived",
        "humanized mouse", "diet-induced obesity", "streptozotocin", "db/db", "ob/ob",
        "high-fat diet", "mdx", "dss colitis", "bleomycin", "ccl4",
    ],
    "计算 / 统计": [
        "machine learning", "deep learning", "neural network", "random forest", "regression model",
        "molecular dynamics", "docking", "structural modeling", "pssm", "position weight matrix",
        "motif analysis", "gene set enrichment", "gsea", "differential expression analysis",
        "deseq2", "edger", "pseudotime", "trajectory analysis", "network analysis",
        "mendelian randomization", "survival analysis", "cox regression",
    ],
}
# 词边界匹配:短缩写(SPR/STED/ASO/FACS...)若用裸子串会命中 "spread"/"tested"/"also",
# 因此统一编译成 \b...\b 正则;含 & . / - 的词做转义后仍保留边界。
def _term_re(term):
    esc = re.escape(term)
    lead = r'(?<![A-Za-z0-9])' if term[0].isalnum() else r''
    tail = r'(?![A-Za-z0-9])' if term[-1].isalnum() else r''
    return re.compile(lead + esc.replace(r'\ ', r'[\s\-]') + tail, re.I)

# 同义/异写归一:否则 "single cell rna" 与 "single-cell rna"、"rna-seq" 与
# "rna sequencing" 会被当成两种不同方法,统计与筛选都会虚高。
CANON = {
    "rna sequencing": "rna-seq", "small rna sequencing": "small rna-seq",
    "single cell rna": "scrna-seq", "single-cell rna": "scrna-seq",
    "ribosome profiling": "ribo-seq", "cryoem": "cryo-em",
    "co-immunoprecipitation": "co-ip", "pulldown": "pull-down",
    "immunoblot": "western blot", "knockin": "knock-in",
    "adeno-associated": "aav", "induced pluripotent": "ipsc",
    "reconstituted": "in vitro reconstitution", "gel shift": "emsa",
    "surface plasmon": "spr", "reporter assay": "luciferase reporter",
    "caenorhabditis": "c. elegans", "porcine": "pig model",
    "patient-derived": "pdx", "in vitro kinase": "kinase assay",
    "crispr-cas9": "crispr", "cas9": "crispr",
    "position weight matrix": "pssm", "differential expression analysis": "deseq2",
    "materials and methods": "methods", "antisense oligonucleotide": "aso",
    "cre/lox": "cre-lox", "x-ray structure": "crystallograph",
}

_METHOD_INDEX = [(m, grp, _term_re(m)) for grp, lst in METHODS.items() for m in lst]


def extract_methods(title, abstract, keywords=None, mesh=None):
    """词典命中 + 返回命中所在原句,便于当场核对。"""
    hay_parts = [title or "", abstract or ""]
    hay_parts += list(keywords or []) + list(mesh or [])
    hay = " ".join(hay_parts)
    sents = split_sentences(abstract)
    out = []
    seen = set()
    for term, grp, rx in _METHOD_INDEX:
        canon = CANON.get(term, term)
        if canon in seen or not rx.search(hay):
            continue
        seen.add(canon)
        ev = next((s for s in sents if rx.search(s)), "")
        out.append({"term": canon, "group": grp, "sent": ev,
                    **({"matched": term} if canon != term else {})})
    # 同组内按词长降序(长词更具体),组间按 METHODS 声明顺序
    order = {g: i for i, g in enumerate(METHODS)}
    out.sort(key=lambda x: (order[x["group"]], -len(x["term"])))
    return out[:18]


# ---------------------------------------------------------------- 主题锚点闸门
# 观察到的失效模式:一篇零 core 关键词、且全文不含任何 miRNA/AMPK 主题词的文章,
# 仅凭两个 peripheral 词 + TF-IDF 余弦噪声就能爬到 medium 档。
# 对策:显式的锚点闸门 —— 无锚点且无 core 命中的记录,相关性档位一律封顶 low,
# 并打 off_topic 标记(不删除,面板上给「隐藏无主题锚点」开关,让读者自己核对)。
ANCHOR_PAT = re.compile(
    r'\b(mi[Rr]NAs?|micro-?RNAs?|miR-\d|let-7|small RNAs?|non-?coding RNAs?|lncRNAs?|siRNAs?'
    r'|RNA-?binding|argonaute|AGO[1-4]\b|dicer|drosha|DGCR8|exportin|XPO5|ZSWIM8|TDMD'
    r'|TUT[47]|ZCCHC11|ZCCHC6|terminal uridylyl|AMPK|PRKAA|RISC|RNA decay|deadenylat'
    r'|3.?UTR|seed (?:sequence|region|match|pairing)|target-directed)\b', re.I)


def anchor_gate(p):
    """返回 (has_anchor, core_hit_n)。同时就地封顶 band。"""
    hay = " ".join([p.get("title") or "", p.get("abstract") or ""]
                   + list(p.get("keywords") or []) + list(p.get("mesh") or []))
    has = bool(ANCHOR_PAT.search(hay))
    core_n = sum(len(d.get("core") or []) for d in (p.get("dirs") or {}).values())
    p["anchor"] = has
    p["core_hits"] = core_n
    if not has and core_n == 0:
        p["off_topic"] = True
        if p.get("band") in ("high", "medium"):
            p["band_raw"] = p["band"]
            p["band"] = "low"
            p["band_capped"] = "无主题锚点(全文无 miRNA/AMPK 类主题词)且无 core 关键词命中 → 档位封顶 low"
    else:
        p["off_topic"] = False
    return has, core_n


# ---------------------------------------------------------------- 语句库(原文句子,按修辞角色)
MOVE_PAT = [
    ("gap", re.compile(
        r'\b(remains?\s+(?:largely\s+)?(?:unclear|unknown|elusive|undefined|to be determined|poorly understood)'
        r'|(?:is|are)\s+(?:still\s+)?(?:largely\s+)?(?:unknown|unclear|undefined|elusive|poorly (?:understood|characteri[sz]ed))'
        r'|(?:has|have)\s+not\s+(?:yet\s+)?been\s+(?:fully\s+)?(?:studied|characteri[sz]ed|determined|addressed|explored|established)'
        r'|little\s+is\s+known|not\s+well\s+understood|lack(?:s|ing)?\s+(?:of\s+)?(?:direct\s+)?evidence'
        r'|no\s+(?:study|studies)\s+(?:has|have)|gap\s+in\s+(?:our\s+)?(?:knowledge|understanding)'
        r'|unresolved|understudied)\b', re.I)),
    ("aim", re.compile(
        r'\b(here\s+we\s+(?:show|report|demonstrate|describe|present|identify|define|use|develop)'
        r'|in\s+this\s+study,?\s+we|we\s+(?:sought|set out|aimed)\s+to|the\s+(?:aim|goal|objective)\s+of\s+this'
        r'|to\s+(?:address|test|determine|investigate)\s+(?:this|whether|how))\b', re.I)),
    ("method", CUE_ME),
    ("result", re.compile(
        r'\b(we\s+(?:found|show|observed|identified|demonstrate|detected|reveal)'
        r'|(?:showed|revealed|demonstrated|indicated)\s+that'
        r'|significantly\s+(?:increased|decreased|reduced|elevated|enhanced|impaired))\b', re.I)),
    ("significance", re.compile(
        r'\b(these\s+(?:results|data|findings)|our\s+(?:results|data|findings)'
        r'|(?:collectively|together|overall|thus|therefore|in\s+summary|in\s+conclusion)'
        r'|provides?\s+(?:a\s+)?(?:new|the\s+first|mechanistic)|highlight(?:s|ing)?'
        r'|(?:may|could|might)\s+(?:represent|serve|offer|provide)|has\s+implications?\s+for'
        r'|(?:identif|reveal)(?:ies|s)?\s+a\s+(?:new|novel|potential)\s+(?:target|mechanism|pathway))\b', re.I)),
    ("question", re.compile(
        r'\b(future\s+(?:studies|work|research|investigations?)|further\s+(?:studies|work|investigation|research)'
        r'|it\s+will\s+be\s+important\s+to|remains?\s+to\s+be\s+(?:determined|established|tested|elucidated)'
        r'|warrants?\s+(?:further\s+)?investigation|open\s+questions?|an\s+important\s+question'
        r'|we\s+propose\s+that|next\s+step)\b', re.I)),
    ("limitation", re.compile(
        r'\b(limitation[s]?\s+of\s+(?:this|our)|a\s+limitation|caveat|we\s+could\s+not'
        r'|were\s+not\s+able\s+to|small\s+sample\s+size|not\s+(?:possible|feasible)\s+to)\b', re.I)),
]
MOVE_ZH = {"gap": "研究空白", "aim": "研究目标", "method": "方法陈述",
           "result": "结果陈述", "significance": "意义/影响", "question": "开放问题",
           "limitation": "局限自陈"}


def phrase_bank(abstract, cap_per_move=2):
    """抽取原文句子并标注修辞角色。只抄,不改写。"""
    sents = split_sentences(abstract)
    sents = [s for s in sents if 30 < len(s) < 420]
    out, used = [], set()
    for move, pat in MOVE_PAT:
        n = 0
        for s in sents:
            if s in used or n >= cap_per_move:
                continue
            if pat.search(s):
                out.append({"move": move, "zh": MOVE_ZH[move], "text": s})
                used.add(s)
                n += 1
    return out


# ---------------------------------------------------------------- 新颖度(三分量,全部可核算)
def novelty(papers, idf, vecs, half_life_days=540):
    """
    对每篇 p 计算三个分量,再加权:
      dist  = 1 − max(与语料中**更早**文献的余弦相似度)   → 越像旧文章越不新
      rare  = 该文 top 词中 IDF 高(语料里罕见)的比例
      fresh = 该文引入的、在更早文献里从未出现过的术语数(归一化)
    没有 LLM 参与;三个分量与最终值一并写进 evidence,供读者核对。
    """
    n = len(papers)
    order = sorted(range(n), key=lambda i: (papers[i].get("date") or "0000-00-00"))
    seen_terms = set()
    first_seen = {}
    # IDF 高分位阈值:语料里出现越少 idf 越大
    idfv = sorted(idf.values())
    hi = idfv[int(len(idfv) * 0.70)] if idfv else 1.0

    # 逐时间顺序推进,只跟"更早"的文献比
    earlier_vecs = []
    EARLIER_CAP = 400          # 与最近 400 篇更早文献比,控制复杂度
    for rank, i in enumerate(order):
        p, v = papers[i], vecs[i]
        top = sorted(v.items(), key=lambda kv: -kv[1])[:40]
        terms = [t for t, _ in top]

        # 分量 1:与更早文献的最大相似度
        best = 0.0
        for ev in earlier_vecs[-EARLIER_CAP:]:
            # 稀疏点积:只遍历本文 top 词
            s = 0.0
            for t, w in top:
                ew = ev.get(t)
                if ew:
                    s += w * ew
            if s > best:
                best = s
        dist = max(0.0, min(1.0, 1.0 - best))

        # 分量 2:稀有词占比
        rare_n = sum(1 for t in terms if idf.get(t, 1.0) >= hi)
        rare = rare_n / max(1, len(terms))

        # 分量 3:首次出现在语料里的术语
        newt = [t for t in terms if t not in seen_terms]
        fresh = min(1.0, len(newt) / 12.0)

        p["_nvraw"] = {"dist": dist, "rare": rare, "fresh": fresh}
        p["novelty_parts"] = {"dist": round(dist, 4), "rare": round(rare, 4),
                              "fresh": round(fresh, 4), "new_terms": newt[:8],
                              "compared_with": min(len(earlier_vecs), EARLIER_CAP)}

        for t in terms:
            if t not in seen_terms:
                seen_terms.add(t)
                first_seen[t] = p.get("date") or ""
        earlier_vecs.append(v)

    # ---- 去掉「语料位置」伪信号 ----------------------------------------
    # fresh 分量天生偏向时间靠前的文献:最早处理的那批看到的是空词表,fresh 必然=1。
    # 实测均值随年份单调下滑(2025→2027),这是排序artifact,不是新颖度。
    # 因此三个分量都先换算成**同年队列内的百分位**,再加权 —— 语义变成
    # 「相对同期文献有多新」,与语料时间跨度无关。
    coh = defaultdict(list)
    for p in papers:
        coh[(p.get("date") or "????")[:4]].append(p)

    def pct_in(vals, x):
        return sum(1 for v in vals if v <= x) / max(1, len(vals))

    for yr, grp in coh.items():
        if len(grp) < 8:            # 队列太小,百分位无意义 → 退回原始值
            for p in grp:
                r = p["_nvraw"]
                p["novelty"] = round(0.5 * r["dist"] + 0.25 * r["rare"] + 0.25 * r["fresh"], 4)
                p["novelty_parts"]["cohort"] = {"year": yr, "n": len(grp), "relative": False}
            continue
        dv = [p["_nvraw"]["dist"] for p in grp]
        rv = [p["_nvraw"]["rare"] for p in grp]
        fv = [p["_nvraw"]["fresh"] for p in grp]
        for p in grp:
            r = p["_nvraw"]
            pd_, pr_, pf_ = pct_in(dv, r["dist"]), pct_in(rv, r["rare"]), pct_in(fv, r["fresh"])
            p["novelty"] = round(0.5 * pd_ + 0.25 * pr_ + 0.25 * pf_, 4)
            p["novelty_parts"]["cohort"] = {"year": yr, "n": len(grp), "relative": True,
                                            "dist_pct": round(pd_ * 100, 1),
                                            "rare_pct": round(pr_ * 100, 1),
                                            "fresh_pct": round(pf_ * 100, 1)}
    for p in papers:
        p.pop("_nvraw", None)

    # 分档用**语料内百分位**,不用写死的绝对阈值 —— 否则语料一换分布就全挤进同一档。
    # 语义因此是明确的相对表述:"在本语料中处于最新颖的 15%"。
    vals = sorted(p.get("novelty", 0.0) for p in papers)
    if vals:
        q85 = vals[min(len(vals) - 1, int(len(vals) * 0.85))]
        q55 = vals[min(len(vals) - 1, int(len(vals) * 0.55))]
    else:
        q85 = q55 = 1.0
    for p in papers:
        nv = p.get("novelty", 0.0)
        p["novelty_band"] = "high" if nv >= q85 else ("medium" if nv >= q55 else "low")
        p["novelty_pct"] = round(100.0 * sum(1 for x in vals if x <= nv) / max(1, len(vals)), 1)
    return papers, {"q85": round(q85, 4), "q55": round(q55, 4), "n": len(vals)}


# ---------------------------------------------------------------- 顶层:给一批 paper 加分析字段
def analyze(papers, idf=None, vecs=None):
    for p in papers:
        anchor_gate(p)
        seg = segment_abstract(p.get("abstract"))
        p["sections"] = seg["sections"]
        p["sections_labeled"] = seg["labeled"]
        p["methods"] = extract_methods(p.get("title"), p.get("abstract"),
                                       p.get("keywords"), p.get("mesh"))
        p["phrases"] = phrase_bank(p.get("abstract"))
    cuts = None
    if idf is not None and vecs is not None:
        papers, cuts = novelty(papers, idf, vecs)
    return papers, cuts


def _standalone():
    """独立运行:对现有 data/papers.json 就地重算分析字段(不重新抓取)。"""
    import importlib.util
    fp = os.path.join(DATA, "papers.json")
    blob = json.load(open(fp))
    papers = blob["papers"]

    # 复用 fetch_papers 的 TF-IDF,保证与线上一致
    spec = importlib.util.spec_from_file_location("fp_mod", os.path.join(HERE, "fetch_papers.py"))
    fpmod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fpmod)
    docs = [fpmod.tokens((p["title"] + " ") * 2 + (p.get("abstract") or "") + " " +
                         " ".join(p.get("mesh", []) + p.get("keywords", [])))
            for p in papers]
    idf, vecs = fpmod.build_tfidf(docs)

    papers, cuts = analyze(papers, idf, vecs)
    blob["meta"]["analysis"] = {
        "novelty_cuts": cuts,
        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "sections_labeled_n": sum(1 for p in papers if p.get("sections_labeled")),
        "sections_inferred_n": sum(1 for p in papers if p.get("sections") and not p.get("sections_labeled")),
        "with_methods_n": sum(1 for p in papers if p.get("methods")),
        "phrase_n": sum(len(p.get("phrases", [])) for p in papers),
        "novelty_band": dict(Counter(p.get("novelty_band") for p in papers)),
        "method_top": Counter(m["term"] for p in papers for m in p.get("methods", [])).most_common(20),
        "anchor_n": sum(1 for p in papers if p.get("anchor")),
        "off_topic_n": sum(1 for p in papers if p.get("off_topic")),
        "band_capped_n": sum(1 for p in papers if p.get("band_capped")),
    }
    json.dump(blob, open(fp, "w"), ensure_ascii=False, separators=(",", ":"))
    print(json.dumps(blob["meta"]["analysis"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    _standalone()
