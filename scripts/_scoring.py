#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_papers.py — 六源文献抓取 + 去重 + 透明相关性评分 + Hotspot/Gap/Question 标记

六个来源(全部免费、无需 API key):
  1. PubMed          E-utilities esearch/efetch      期刊论文 + MeSH
  2. Europe PMC      REST search resultType=core     摘要/OA状态/PDF/被引数/单位
  3. bioRxiv         api.biorxiv.org/details         预印本(当日最新,含 published 字段)
  4. medRxiv         经 Europe PMC SRC:PPR 索引       (api.medrxiv.org 在本环境不可达)
  5. arXiv           export.arxiv.org/api/query      q-bio / 计算方法
  6. Crossref        api.crossref.org/works          DOI 元数据 + 被引数(补 PubMed 未收录刊)

不使用:Web of Science(需付费订阅)、Google Scholar(无官方 API,抓取违反 ToS)、
       JCR 影响因子(Clarivate 版权数据,无免费接口)。见 README「已知局限」。

输出:
  data/papers.json              主数据(去重后,带评分与标记)
  data/citation_snapshots.json  每日被引快照(累积,用于日后真实计算引用加速度)
  data/fetch_log_papers.json    每源抓取日志(命中数/耗时/错误)
  data/digest.json              当日摘要
  feed.xml                      RSS
"""
import json, os, re, sys, time, math, urllib.request, urllib.parse, datetime, difflib
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
UA = "grants-finder-paper-tracker/1.0"
# NCBI/EBI 礼貌参数:仅当环境变量存在时附带,脚本内不硬编码任何邮箱
CONTACT = os.environ.get("NCBI_EMAIL", "").strip()
WINDOW_DAYS = int(os.environ.get("PAPER_WINDOW_DAYS", "540"))   # 抓取时间窗
MAX_PER_SOURCE = int(os.environ.get("PAPER_MAX_PER_SOURCE", "400"))
TODAY = datetime.date.today()
SINCE = TODAY - datetime.timedelta(days=WINDOW_DAYS)

LOG = []                   # 抓取日志
BIORXIV_STAT = {}          # bioRxiv 扫描覆盖率(诚实报告是否截断)
def logrow(src, direction, n, sec, err=None):
    LOG.append({"source": src, "direction": direction, "n": n,
                "seconds": round(sec, 2), "error": err})
    tag = "OK " if not err else "ERR"
    print(f"  [{tag}] {src:10s} {direction:11s} n={n:<5d} {sec:5.1f}s" + (f"  {err}" if err else ""),
          flush=True)


# ---------------------------------------------------------------- HTTP
def http(url, timeout=45, tries=3, accept="application/json"):
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            time.sleep(1.5 * (k + 1))
    raise last

def jget(url, **kw):
    return json.loads(http(url, **kw))


# ---------------------------------------------------------------- 归一化
def norm_doi(d):
    if not d: return ""
    d = str(d).strip().lower()
    d = re.sub(r'^https?://(dx\.)?doi\.org/', '', d)
    return d.rstrip('.')

def norm_title(t):
    if not t: return ""
    t = re.sub(r'<[^>]+>', ' ', str(t))            # 去 <i> 等标签
    t = re.sub(r'&[a-z]+;|&#\d+;', ' ', t)
    t = re.sub(r'[^a-z0-9 ]+', ' ', t.lower())
    return re.sub(r'\s+', ' ', t).strip()

def clean_abs(a):
    if not a: return ""
    a = re.sub(r'<[^>]+>', ' ', str(a))
    a = re.sub(r'\s+', ' ', a)
    return a.strip()

# 上游元数据脏数据兜底:Crossref 实测返回过 issued=2101-11-15 这类日期。
# 这种记录会排在「最新」视图首位,并单独构成一个 1 条的年度队列,
# 把 novelty 的同年队列百分位打穿。统一在 rec() 里夹逼,原始串保留到
# date_raw 以便审计 —— 不静默丢弃。
# Crossref 常只给到年或年月(2026-12 / 2027),这些是合法的,必须补全而不是丢弃 ——
# 一刀切要求 YYYY-MM-DD 会误杀 112/1097 条真实记录(实测)。
DATE_RE = re.compile(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?$")


def plausible_date(s, max_ahead_days=400):
    """规范化为 YYYY-MM-DD;不可信返回 None。缺失的月/日补 01。"""
    mo = DATE_RE.match((s or "").strip()[:10])
    if not mo:
        return None
    y, mm, dd = mo.group(1), mo.group(2) or "1", mo.group(3) or "1"
    try:
        d = datetime.date(int(y), int(mm), int(dd))
    except ValueError:
        return None
    if d.year < 1900 or d > TODAY + datetime.timedelta(days=max_ahead_days):
        return None
    return d.isoformat()


def rec(**kw):
    """统一记录结构。"""
    d = {"pmid": "", "doi": "", "title": "", "abstract": "", "journal": "", "authors": [],
         "affil": "", "date": "", "year": None, "src": [], "url": "", "pdf": "",
         "is_oa": None, "cites": None, "ptype": "article", "mesh": [], "keywords": [],
         "preprint": False, "published_as": "", "category": ""}
    d.update(kw)
    raw = d.get("date") or ""
    if raw:
        ok = plausible_date(raw)
        if ok is None:
            d["date_raw"] = raw       # 审计用:保留上游原值
            d["date"] = ""
            d["year"] = None
        else:
            d["date"] = ok
            if d.get("year") and str(d["year"]) != ok[:4]:
                d["year"] = int(ok[:4])
    return d


# ---------------------------------------------------------------- 1) PubMed
def fetch_pubmed(direction, query):
    t0 = time.time(); out = []
    try:
        base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
        extra = f"&email={urllib.parse.quote(CONTACT)}" if CONTACT else ""
        u = (base + "esearch.fcgi?db=pubmed&retmode=json&retmax=" + str(MAX_PER_SOURCE) +
             "&datetype=pdat&mindate=" + SINCE.strftime("%Y/%m/%d") +
             "&maxdate=" + TODAY.strftime("%Y/%m/%d") +
             "&term=" + urllib.parse.quote(query) + extra)
        ids = jget(u)["esearchresult"].get("idlist", [])
        for i in range(0, len(ids), 200):
            chunk = ids[i:i + 200]
            xml = http(base + "efetch.fcgi?db=pubmed&retmode=xml&id=" + ",".join(chunk) + extra,
                       accept="application/xml")
            root = ET.fromstring(xml)
            for art in root.findall(".//PubmedArticle"):
                out.append(_parse_pubmed_article(art))
            time.sleep(0.4)
        logrow("pubmed", direction, len(out), time.time() - t0)
    except Exception as e:
        logrow("pubmed", direction, len(out), time.time() - t0, f"{type(e).__name__}: {e}")
    return out

def _parse_pubmed_article(art):
    def tx(p):
        el = art.find(p)
        return (el.text or "").strip() if el is not None and el.text else ""
    pmid = tx(".//PMID")
    ttl_el = art.find(".//ArticleTitle")
    title = "".join(ttl_el.itertext()).strip() if ttl_el is not None else ""
    abs_parts = ["".join(a.itertext()).strip() for a in art.findall(".//AbstractText")]
    doi = ""
    for aid in art.findall(".//ArticleId"):
        if aid.get("IdType") == "doi":
            doi = norm_doi(aid.text)
    authors, affil = [], ""
    for a in art.findall(".//Author")[:30]:
        ln = a.findtext("LastName") or ""
        ini = a.findtext("Initials") or ""
        if ln:
            authors.append((ln + " " + ini).strip())
        if not affil:
            af = a.findtext(".//Affiliation")
            if af: affil = af[:300]
    y = art.findtext(".//PubDate/Year") or art.findtext(".//ArticleDate/Year") or ""
    m = art.findtext(".//PubDate/Month") or art.findtext(".//ArticleDate/Month") or "01"
    dd = art.findtext(".//ArticleDate/Day") or "01"
    MON = {"jan":"01","feb":"02","mar":"03","apr":"04","may":"05","jun":"06",
           "jul":"07","aug":"08","sep":"09","oct":"10","nov":"11","dec":"12"}
    m = MON.get(m[:3].lower(), m if m.isdigit() else "01")
    date = f"{y}-{str(m).zfill(2)}-{str(dd).zfill(2)}" if y else ""
    mesh = [x.findtext("DescriptorName") or "" for x in art.findall(".//MeshHeading")]
    kws = [(k.text or "").strip() for k in art.findall(".//Keyword")]
    ptl = [(p.text or "").lower() for p in art.findall(".//PublicationType")]
    ptype = "review" if any("review" in p for p in ptl) else "article"
    return rec(pmid=pmid, doi=doi, title=title, abstract=clean_abs(" ".join(abs_parts)),
               journal=tx(".//Journal/Title"), authors=authors, affil=affil, date=date,
               year=int(y) if y.isdigit() else None, src=["pubmed"], ptype=ptype,
               mesh=[x for x in mesh if x], keywords=[k for k in kws if k],
               url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "")


# ---------------------------------------------------------------- 2/4) Europe PMC(含 medRxiv 等预印本)
def fetch_epmc(direction, query, preprints=False):
    t0 = time.time(); out = []
    label = "epmc_ppr" if preprints else "epmc"
    try:
        q = f'({query}) AND FIRST_PDATE:[{SINCE} TO {TODAY}]'
        q += ' AND (SRC:PPR)' if preprints else ' AND (SRC:MED OR SRC:PMC)'
        cursor = "*"
        while len(out) < MAX_PER_SOURCE:
            u = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search?format=json"
                 "&resultType=core&pageSize=100&cursorMark=" + urllib.parse.quote(cursor) +
                 "&query=" + urllib.parse.quote(q))
            j = jget(u)
            res = j.get("resultList", {}).get("result", [])
            if not res: break
            for r in res:
                out.append(_parse_epmc(r, preprints))
            nxt = j.get("nextCursorMark")
            if not nxt or nxt == cursor: break
            cursor = nxt
            time.sleep(0.3)
        logrow(label, direction, len(out), time.time() - t0)
    except Exception as e:
        logrow(label, direction, len(out), time.time() - t0, f"{type(e).__name__}: {e}")
    return out

def _parse_epmc(r, preprints=False):
    ft = (r.get("fullTextUrlList") or {}).get("fullTextUrl", []) or []
    pdf = next((x.get("url") for x in ft
                if x.get("documentStyle") == "pdf" and x.get("availability") == "Open access"), "")
    html = next((x.get("url") for x in ft if x.get("documentStyle") == "html"), "")
    ji = r.get("journalInfo") or {}
    jt = ((ji.get("journal") or {}).get("title")) or r.get("journalTitle") or ""
    if preprints and not jt:
        jt = (r.get("bookOrReportDetails") or {}).get("publisher") or "Preprint"
    auth = [(a.get("lastName", "") + " " + a.get("initials", "")).strip()
            for a in ((r.get("authorList") or {}).get("author") or [])[:30]]
    af = ""
    for a in ((r.get("authorList") or {}).get("author") or []):
        al = ((a.get("authorAffiliationDetailsList") or {}).get("authorAffiliation") or [])
        if al:
            af = (al[0].get("affiliation") or "")[:300]; break
    if not af: af = (r.get("affiliation") or "")[:300]
    mesh = [(m.get("descriptorName") or "") for m in
            ((r.get("meshHeadingList") or {}).get("meshHeading") or [])]
    kws = ((r.get("keywordList") or {}).get("keyword") or [])
    pts = [p.lower() for p in ((r.get("pubTypeList") or {}).get("pubType") or [])]
    ptype = "preprint" if preprints else ("review" if any("review" in p for p in pts) else "article")
    cid = r.get("id") or ""
    return rec(pmid=r.get("pmid") or "", doi=norm_doi(r.get("doi")),
               title=clean_abs(r.get("title")), abstract=clean_abs(r.get("abstractText")),
               journal=jt, authors=auth, affil=af,
               date=r.get("firstPublicationDate") or "",
               year=int(r["pubYear"]) if str(r.get("pubYear", "")).isdigit() else None,
               src=["epmc_ppr" if preprints else "epmc"],
               url=html or (f"https://europepmc.org/article/{r.get('source','MED')}/{cid}"),
               pdf=pdf, is_oa=(r.get("isOpenAccess") == "Y"),
               cites=r.get("citedByCount"), ptype=ptype, preprint=preprints,
               mesh=[m for m in mesh if m], keywords=[k for k in kws if k])


# ---------------------------------------------------------------- 3) bioRxiv
def fetch_biorxiv(all_terms, days=45, budget_s=240, max_pages=60):
    """bioRxiv 官方 API 只支持按日期窗取全量(每页 30 条),故本地按词库过滤。

    诚实说明:bioRxiv 每 45 天约有 1.2 万篇预印本,全量翻页要几百次请求。
    这里设 budget_s 秒预算 + 页数上限,超出即停并在日志里记录扫描进度 ——
    宁可漏掉一部分旧预印本,也不让每日 Actions 任务卡住。
    最新的预印本排在前面,所以先扫到的正是最该看的。
    Europe PMC 的 SRC:PPR 源(含 medRxiv/Research Square)是本源的补充覆盖。
    """
    t0 = time.time(); out = []; scanned = 0; truncated = False; total = 0
    try:
        start = (TODAY - datetime.timedelta(days=days)).isoformat()
        for server in ("biorxiv",):
            cursor = 0
            pages = 0
            while True:
                if time.time() - t0 > budget_s or pages >= max_pages:
                    truncated = True; break
                pages += 1
                try:
                    # 预算必须是硬截止:urlopen 的 timeout 是"每次 socket 操作"而非总时长,
                    # 慢速涓流响应可以远超它,所以这里按剩余预算收紧 timeout 且不重试。
                    left = budget_s - (time.time() - t0)
                    j = jget(f"https://api.{server}.org/details/{server}/{start}/{TODAY.isoformat()}/{cursor}",
                             timeout=max(5, min(15, left)), tries=1)
                except Exception as e:
                    truncated = True
                    logrow("biorxiv", f"page{cursor}", 0, time.time() - t0, f"分页中断: {type(e).__name__}")
                    break
                coll = j.get("collection") or []
                if not coll: break
                for c in coll:
                    scanned += 1
                    blob = (str(c.get("title", "")) + " " + str(c.get("abstract", ""))).lower()
                    if not any(t in blob for t in all_terms):
                        continue
                    out.append(rec(
                        doi=norm_doi(c.get("doi")), title=clean_abs(c.get("title")),
                        abstract=clean_abs(c.get("abstract")),
                        journal=("bioRxiv" if server == "biorxiv" else "medRxiv"),
                        authors=[a.strip() for a in str(c.get("authors", "")).split(";")[:30] if a.strip()],
                        affil=(c.get("author_corresponding_institution") or "")[:300],
                        date=c.get("date", ""),
                        year=int(str(c.get("date", ""))[:4]) if str(c.get("date", ""))[:4].isdigit() else None,
                        src=[server], url="https://doi.org/" + norm_doi(c.get("doi")),
                        pdf=f"https://www.{server}.org/content/{norm_doi(c.get('doi'))}v{c.get('version','1')}.full.pdf",
                        is_oa=True, ptype="preprint", preprint=True,
                        published_as=(c.get("published") or "") if str(c.get("published","")).lower() not in ("na","") else "",
                        category=c.get("category", "")))
                total = int(j.get("messages", [{}])[0].get("total", 0) or 0)
                cursor += len(coll)
                if cursor >= total or cursor >= 6000: break
                time.sleep(0.15)
        BIORXIV_STAT.update(scanned=scanned, total=total, truncated=truncated)
        logrow("biorxiv", f"scan{scanned}" + ("+cut" if truncated else ""),
               len(out), time.time() - t0)
    except Exception as e:
        logrow("biorxiv", f"scan{scanned}", len(out), time.time() - t0, f"{type(e).__name__}: {e}")
    return out


# ---------------------------------------------------------------- 5) arXiv
def fetch_arxiv(direction, query):
    """arXiv 服务条款要求请求之间至少间隔 3 秒;违反会收到 429。
    本站 arXiv 命中量很小(生物学论文极少投 arXiv),故失败即跳过、不重试拖慢全局。"""
    t0 = time.time(); out = []
    try:
        time.sleep(3.5)
        NS = "{http://www.w3.org/2005/Atom}"
        u = ("https://export.arxiv.org/api/query?search_query=" + urllib.parse.quote(query) +
             "&max_results=80&sortBy=submittedDate&sortOrder=descending")
        root = ET.fromstring(http(u, accept="application/atom+xml", timeout=25, tries=1))
        for e in root.findall(NS + "entry"):
            pub = (e.findtext(NS + "published") or "")[:10]
            if pub and pub < SINCE.isoformat():
                continue
            aid = (e.findtext(NS + "id") or "")
            pdf = next((l.get("href") for l in e.findall(NS + "link") if l.get("title") == "pdf"), "")
            out.append(rec(
                doi=norm_doi(e.findtext(NS + "doi") or ""),
                title=clean_abs(e.findtext(NS + "title")),
                abstract=clean_abs(e.findtext(NS + "summary")),
                journal="arXiv",
                authors=[(a.findtext(NS + "name") or "").strip() for a in e.findall(NS + "author")][:30],
                date=pub, year=int(pub[:4]) if pub[:4].isdigit() else None,
                src=["arxiv"], url=aid, pdf=pdf, is_oa=True,
                ptype="preprint", preprint=True,
                category=(e.find(NS + "category").get("term") if e.find(NS + "category") is not None else "")))
        logrow("arxiv", direction, len(out), time.time() - t0)
    except Exception as e:
        logrow("arxiv", direction, len(out), time.time() - t0, f"{type(e).__name__}: {e}")
    return out


# ---------------------------------------------------------------- 6) Crossref
def fetch_crossref(direction, phrase):
    t0 = time.time(); out = []
    try:
        extra = f"&mailto={urllib.parse.quote(CONTACT)}" if CONTACT else ""
        u = ("https://api.crossref.org/works?rows=100&sort=published&order=desc"
             "&select=DOI,title,abstract,container-title,author,issued,is-referenced-by-count,type,URL,link"
             "&filter=" + urllib.parse.quote(f"from-pub-date:{SINCE},type:journal-article") +
             "&query.bibliographic=" + urllib.parse.quote(phrase) + extra)
        for it in jget(u)["message"]["items"]:
            ttl = (it.get("title") or [""])[0]
            if not ttl: continue
            dp = ((it.get("issued") or {}).get("date-parts") or [[None]])[0]
            date = "-".join(str(x).zfill(2) for x in dp if x) if dp and dp[0] else ""
            auth = [((a.get("family") or "") + " " + (a.get("given") or "")[:1]).strip()
                    for a in (it.get("author") or [])[:30]]
            out.append(rec(
                doi=norm_doi(it.get("DOI")), title=clean_abs(ttl),
                abstract=clean_abs(it.get("abstract")),
                journal=(it.get("container-title") or [""])[0],
                authors=auth, date=date,
                year=dp[0] if dp and str(dp[0]).isdigit() else None,
                src=["crossref"], url=it.get("URL") or "",
                cites=it.get("is-referenced-by-count"), ptype="article"))
        logrow("crossref", direction, len(out), time.time() - t0)
    except Exception as e:
        logrow("crossref", direction, len(out), time.time() - t0, f"{type(e).__name__}: {e}")
    return out


# ---------------------------------------------------------------- 去重
def dedupe(records):
    """三级去重:归一化 DOI → PMID → 标题相似度(difflib ≥0.92)。
    合并时保留字段最全的版本,来源标签取并集。"""
    by_doi, by_pmid, rest = {}, {}, []
    merged = []
    def richness(r):
        return (len(r.get("abstract") or "") + 50 * bool(r.get("pdf")) +
                30 * bool(r.get("mesh")) + 20 * len(r.get("authors") or []) +
                10 * bool(r.get("affil")) + (5 if r.get("cites") is not None else 0))
    def merge_into(a, b):
        if richness(b) > richness(a):
            a, b = b, a
        for k, v in b.items():
            if k == "src":
                a["src"] = sorted(set(a["src"]) | set(v))
            elif k in ("cites",):
                if v is not None:
                    a["cites"] = max(a["cites"] or 0, v)
            elif k in ("mesh", "keywords"):
                a[k] = sorted(set((a.get(k) or [])) | set(v or []))
            elif k == "preprint":
                # 布尔字段不能用 "not a.get(k)" 判空:预印本与其期刊版本合并后,
                # 期刊版(preprint=False)会被预印本版(True)覆盖,导致 Genes & Dev 论文被标成预印本。
                # 正确语义:只要任一版本已正式发表,合并结果就不是预印本。
                a["preprint"] = bool(a.get("preprint")) and bool(v)
            elif not a.get(k) and v:
                a[k] = v
        return a
    for r in records:
        d, p = r.get("doi"), r.get("pmid")
        if d and d in by_doi:
            by_doi[d] = merge_into(by_doi[d], r); continue
        if p and p in by_pmid:
            by_pmid[p] = merge_into(by_pmid[p], r); continue
        if d: by_doi[d] = r
        elif p: by_pmid[p] = r
        else: rest.append(r)
    pool = list(by_doi.values()) + [v for k, v in by_pmid.items()
                                    if not any(v is x for x in by_doi.values())] + rest
    # 标题相似度(按标题首 12 字符分桶,避免 O(n²))
    buckets = defaultdict(list)
    for r in pool:
        buckets[norm_title(r["title"])[:12]].append(r)
    seen = set()
    for key, group in buckets.items():
        used = [False] * len(group)
        for i, a in enumerate(group):
            if used[i]: continue
            for j in range(i + 1, len(group)):
                if used[j]: continue
                ta, tb = norm_title(a["title"]), norm_title(group[j]["title"])
                if ta and tb and difflib.SequenceMatcher(None, ta, tb).ratio() >= 0.92:
                    a = merge_into(a, group[j]); used[j] = True
            merged.append(a); used[i] = True
    # 预印本→正式发表链接(同一 DOI 前缀或标题相同)
    pubs = {norm_title(r["title"]): r for r in merged if not r["preprint"]}
    for r in merged:
        if r["preprint"] and norm_title(r["title"]) in pubs:
            r["published_as"] = pubs[norm_title(r["title"])].get("doi") or ""
    return merged


# ---------------------------------------------------------------- TF-IDF
def build_tfidf(docs):
    """纯 Python TF-IDF:docs = [token list]。返回 (idf, doc_vecs)。"""
    N = len(docs) or 1
    df = Counter()
    for toks in docs:
        df.update(set(toks))
    idf = {t: math.log((N + 1) / (c + 1)) + 1.0 for t, c in df.items()}
    vecs = []
    for toks in docs:
        tf = Counter(toks)
        v = {t: (1 + math.log(c)) * idf.get(t, 1.0) for t, c in tf.items()}
        nrm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vecs.append({t: x / nrm for t, x in v.items()})
    return idf, vecs

STOP = set("""a an the of and or in on for to with by from as is are was were be been being that this these those
we our it its at not no than then thus which who whom whose how what when where why can could may might will would
should shall do does did done have has had having but if so such between among during within without into onto
about above below over under also more most less least very much many few both each other same own only just
however therefore moreover furthermore here there their them they he she his her you your i me my""".split())

def tokens(text):
    return [w for w in re.findall(r'[a-z][a-z0-9\-]{2,}', (text or "").lower())
            if w not in STOP and len(w) > 2]


# ---------------------------------------------------------------- 评分
def score_papers(papers, cfg):
    W = cfg["score_weights"]; BANDS = cfg["bands"]
    EXCL = [e.lower() for e in cfg.get("exclude", [])]
    ints = cfg["interests"]
    docs = [tokens((p["title"] + " ") * 2 + p["abstract"] + " " + " ".join(p.get("mesh", []) + p.get("keywords", [])))
            for p in papers]
    idf, vecs = build_tfidf(docs)
    # 每方向的 TF-IDF 画像向量(core 权重 1.0,peri 0.35)
    prof = {}
    for it in ints:
        v = {}
        for term in it["core"]:
            for t in tokens(term): v[t] = v.get(t, 0) + 1.0 * idf.get(t, 1.0)
        for term in it["peri"]:
            for t in tokens(term): v[t] = v.get(t, 0) + W["peri_hit"] * idf.get(t, 1.0)
        nrm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        prof[it["id"]] = {t: x / nrm for t, x in v.items()}

    for p, vec in zip(papers, vecs):
        tl = (p["title"] or "").lower()
        bl = (p["title"] + " " + p["abstract"] + " " + " ".join(p.get("mesh", []) + p.get("keywords", []))).lower()
        per_dir, hits_all = {}, []
        for it in ints:
            core_hits = [t for t in it["core"] if t in bl]
            peri_hits = [t for t in it["peri"] if t in bl]
            kw = 0.0
            for t in core_hits:
                kw += W["core_hit"] * (W["title_multiplier"] if t in tl else 1.0)
            for t in peri_hits:
                kw += W["peri_hit"] * (W["title_multiplier"] if t in tl else 1.0)
            kw_n = kw / (len(it["core"]) * W["core_hit"] * 1.2) if it["core"] else 0.0
            kw_n = min(1.0, kw_n)
            cos = sum(vec.get(t, 0) * w for t, w in prof[it["id"]].items())
            s = (W["keyword"] * kw_n + W["tfidf"] * min(1.0, cos * 2.2)) * it["w"]
            if s > 0.01:
                per_dir[it["id"]] = {"score": round(s, 4), "core": core_hits[:6],
                                     "peri": peri_hits[:6], "cos": round(cos, 4),
                                     "kw": round(kw_n, 4)}
                hits_all += core_hits + peri_hits
        pen = sum(W["exclude_penalty"] for e in EXCL if e in bl)
        best = max(per_dir.items(), key=lambda kv: kv[1]["score"], default=(None, {"score": 0}))
        total = max(0.0, best[1]["score"] + 0.15 * (len(per_dir) - 1) + pen * 0.2)
        p["score"] = round(min(1.0, total), 4)
        p["band"] = "high" if p["score"] >= BANDS["high"] else ("medium" if p["score"] >= BANDS["medium"] else "low")
        p["dirs"] = per_dir
        p["top_dir"] = best[0] or ""
        p["hits"] = sorted(set(hits_all))[:12]
        p["penalized"] = pen < 0
    return papers


# ---------------------------------------------------------------- Gap / Question 句抽取
GAP_PAT = re.compile(
    r'\b(remains?\s+(?:largely\s+)?(?:unclear|unknown|elusive|undefined|to be determined|poorly understood)'
    r'|(?:is|are)\s+(?:still\s+)?(?:largely\s+)?(?:unknown|unclear|undefined|elusive|poorly (?:understood|characteri[sz]ed))'
    r'|(?:has|have)\s+not\s+(?:yet\s+)?been\s+(?:fully\s+)?(?:studied|characteri[sz]ed|determined|addressed|explored|established)'
    r'|little\s+is\s+known|not\s+well\s+understood|lack(?:s|ing)?\s+(?:of\s+)?(?:direct\s+)?evidence'
    r'|no\s+(?:study|studies)\s+(?:has|have)|gap\s+in\s+(?:our\s+)?(?:knowledge|understanding)'
    r'|whether\s+.{0,60}\s+remains|unresolved|understudied)\b', re.I)
Q_PAT = re.compile(
    r'\b(future\s+(?:studies|work|research|investigations?)|further\s+(?:studies|work|investigation|research)'
    r'|it\s+will\s+be\s+important\s+to|remains?\s+to\s+be\s+(?:determined|established|tested|elucidated)'
    r'|warrants?\s+(?:further\s+)?investigation|should\s+be\s+(?:further\s+)?(?:investigated|explored|tested)'
    r'|open\s+questions?|an\s+important\s+question|we\s+propose\s+that|next\s+step)\b', re.I)
LIM_PAT = re.compile(
    r'\b(limitation[s]?\s+of\s+(?:this|our)|a\s+limitation|caveat|we\s+could\s+not|were\s+not\s+able\s+to'
    r'|small\s+sample\s+size|not\s+(?:possible|feasible)\s+to)\b', re.I)

def sentences(t):
    t = re.sub(r'\s+', ' ', t or "")
    return [s.strip() for s in re.split(r'(?<=[.!?])\s+(?=[A-Z(])', t) if 25 < len(s.strip()) < 400]

def mark_paper(p, snap_prev, snap_days):
    """三类标记 —— 全部来自当日可核验的字段,不编造。"""
    tags, ev = [], {}
    sents = sentences(p["abstract"])
    gaps = [s for s in sents if GAP_PAT.search(s)][:2]
    qs   = [s for s in sents if Q_PAT.search(s)][:2]
    lims = [s for s in sents if LIM_PAT.search(s)][:1]
    if gaps: tags.append("gap");      ev["gap"] = gaps
    if qs:   tags.append("question"); ev["question"] = qs
    if lims: ev["limitation"] = lims

    # Hotspot:引用速率(可当日核验)+ 新鲜度 + 预印本转正
    cites = p.get("cites") or 0
    try:
        d0 = datetime.date.fromisoformat((p.get("date") or "")[:10])
        months = max(0.7, (TODAY - d0).days / 30.44)
        age_days = (TODAY - d0).days
    except Exception:
        months, age_days = 12.0, 999
    rate = cites / months
    p["cite_rate"] = round(rate, 3)
    p["age_days"] = age_days
    # 真实加速度:仅当已有历史快照才计算,否则为 None
    accel = None
    key = p.get("doi") or p.get("pmid")
    if key and key in snap_prev and snap_days >= 7:
        prev_c, prev_days = snap_prev[key]
        if prev_days >= 5:
            accel = round((cites - prev_c) / (prev_days / 30.44), 3)
    p["cite_accel"] = accel
    p["snap_days"] = snap_days

    hot = 0.0
    if rate >= 3: hot += 0.45
    elif rate >= 1: hot += 0.3
    elif rate >= 0.4: hot += 0.15
    if age_days <= 60: hot += 0.3
    elif age_days <= 180: hot += 0.15
    if p.get("published_as"): hot += 0.15          # 预印本已被期刊接收 = 同行认可信号
    if accel is not None and accel >= 1.5: hot += 0.25
    hot = round(min(1.0, hot), 3)      # 先取整:0.3+0.15 在浮点下 = 0.44999… < 0.45,会让阈值永不触发
    p["hot_score"] = hot
    if hot >= 0.45 and p.get("score", 0) >= 0.28:
        tags.append("hotspot")
        ev["hotspot"] = {"cite_rate": p["cite_rate"], "cites": cites,
                         "age_days": age_days, "accel": accel,
                         "published_as": p.get("published_as") or None}
    p["tags"] = tags
    p["evidence"] = ev
    return p


# ---------------------------------------------------------------- 期刊层级代理(非 JCR IF)
TIER = {
    1: ["nature", "science", "cell", "new england journal", "lancet"],
    2: ["nature genetics", "nature cell biology", "nature structural", "nature metabolism",
        "nature communications", "molecular cell", "cell metabolism", "cell reports",
        "immunity", "neuron", "developmental cell", "cancer cell", "genes & development",
        "embo journal", "journal of clinical investigation", "science advances", "elife",
        "nucleic acids research", "pnas", "proceedings of the national academy"],
    3: ["rna", "rna biology", "journal of biological chemistry", "plos genetics",
        "plos biology", "embo reports", "diabetes", "diabetologia", "hepatology",
        "circulation research", "cardiovascular research", "gut", "journal of hepatology",
        "molecular therapy", "nar", "bioinformatics", "genome biology", "genome research"],
}
def journal_tier(j):
    jl = (j or "").lower()
    if not jl: return None
    # 用子串匹配:真实刊名常带后缀,如 "bioRxiv : the preprint server for biology"
    if any(k in jl for k in ("biorxiv", "medrxiv", "arxiv", "research square",
                             "preprint", "ssrn", "authorea")): return "preprint"
    for t in (1, 2, 3):
        for name in TIER[t]:
            if name in jl:
                # 一级刊名是二级刊名的子串(如 "nature" ⊂ "nature genetics"),故先查更长的
                if t == 1 and any(n2 in jl for n2 in TIER[2] if len(n2) > len(name)):
                    return "T2"
                return f"T{t}"
    return "T4"


# ---------------------------------------------------------------- 引用快照
def load_snapshots():
    fp = os.path.join(DATA, "citation_snapshots.json")
    if os.path.exists(fp):
        try: return json.load(open(fp))
        except Exception: pass
    return {"first_date": TODAY.isoformat(), "snapshots": {}}

def save_snapshots(snap, papers):
    day = TODAY.isoformat()
    snap["snapshots"][day] = {(p.get("doi") or p.get("pmid")): (p.get("cites") or 0)
                              for p in papers if (p.get("doi") or p.get("pmid")) and p.get("cites") is not None}
    # 只保留最近 120 天,控制体积
    keys = sorted(snap["snapshots"].keys())[-120:]
    snap["snapshots"] = {k: snap["snapshots"][k] for k in keys}
    snap["days_accumulated"] = len(snap["snapshots"])
    json.dump(snap, open(os.path.join(DATA, "citation_snapshots.json"), "w"),
              ensure_ascii=False, separators=(",", ":"))
    return snap

def prev_snapshot(snap):
    """返回 {key: (cites, days_ago)} —— 取最早一份可用快照做基线。"""
    days = sorted(snap.get("snapshots", {}).keys())
    if not days: return {}, 0
    oldest = days[0]
    try:
        n = (TODAY - datetime.date.fromisoformat(oldest)).days
    except Exception:
        n = 0
    return {k: (v, n) for k, v in snap["snapshots"][oldest].items()}, len(days)


# ---------------------------------------------------------------- 主流程
def main():
    cfg = json.load(open(os.path.join(DATA, "interests.json")))
    ints = cfg["interests"]
    all_terms = sorted({t.lower() for it in ints for t in (it["core"] + it["peri"]) if len(t) > 4})
    print(f"== fetch_papers  window {SINCE} → {TODAY}  ({WINDOW_DAYS}d), {len(ints)} directions ==", flush=True)

    raw = []
    for it in ints:
        raw += fetch_pubmed(it["id"], it["q_pubmed"])
        raw += fetch_epmc(it["id"], it["q_epmc"])
        raw += fetch_epmc(it["id"], it["q_epmc"], preprints=True)
        if it.get("q_arxiv"):
            raw += fetch_arxiv(it["id"], it["q_arxiv"])
        if it.get("q_crossref"):
            raw += fetch_crossref(it["id"], it["q_crossref"])
    raw += fetch_biorxiv(all_terms, days=60)
    print(f"raw records: {len(raw)}", flush=True)

    papers = dedupe(raw)
    print(f"after dedupe: {len(papers)}  (removed {len(raw) - len(papers)})", flush=True)

    papers = score_papers(papers, cfg)
    snap = load_snapshots()
    prev, snap_days = prev_snapshot(snap)
    for p in papers:
        mark_paper(p, prev, snap_days)
        p["tier"] = journal_tier(p["journal"])
    # 只保留有相关性的
    papers = [p for p in papers if p["score"] >= 0.12]
    papers.sort(key=lambda p: (-p["score"], p.get("date", "")), reverse=False)
    papers.sort(key=lambda p: (p["score"], p.get("date", "")), reverse=True)
    for i, p in enumerate(papers): p["i"] = i

    snap = save_snapshots(snap, papers)
    meta = {
        "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "window": {"since": SINCE.isoformat(), "until": TODAY.isoformat(), "days": WINDOW_DAYS},
        "n": len(papers), "n_raw": len(raw),
        "by_src": dict(Counter(s for p in papers for s in p["src"])),
        "by_band": dict(Counter(p["band"] for p in papers)),
        "by_dir": dict(Counter(p["top_dir"] for p in papers)),
        "by_tag": dict(Counter(t for p in papers for t in p["tags"])),
        "oa_n": sum(1 for p in papers if p.get("is_oa")),
        "preprint_n": sum(1 for p in papers if p["preprint"]),
        "snapshot_days": snap.get("days_accumulated", 0),
        "snapshot_first": snap.get("first_date"),
        "sources": ["pubmed", "epmc", "epmc_ppr", "biorxiv", "arxiv", "crossref"],
        "biorxiv_scan": dict(BIORXIV_STAT),
        "excluded_sources": {"web_of_science": "需付费订阅",
                             "google_scholar": "无官方 API,抓取违反 ToS",
                             "jcr_impact_factor": "Clarivate 版权数据,无免费接口;本站用期刊层级 T1–T4 代理并如实标注",
                             "openalex": "需 API key(未授权);被引数改由 Europe PMC + Crossref 提供"},
    }
    json.dump({"meta": meta, "papers": papers},
              open(os.path.join(DATA, "papers.json"), "w"), ensure_ascii=False, separators=(",", ":"))
    json.dump({"updated": meta["updated"], "log": LOG},
              open(os.path.join(DATA, "fetch_log_papers.json"), "w"), ensure_ascii=False, indent=1)
    print(json.dumps({k: meta[k] for k in ("n", "n_raw", "by_band", "by_tag", "snapshot_days")},
                     ensure_ascii=False))
    return papers, meta


if __name__ == "__main__":
    main()
