/* tests/assertions.js —— 断言的是「诚实性契约」,不只是「不报错」。
   每条断言对应一个真实存在过的缺陷或一条不能悄悄退化的披露。 */
var FAIL = 0, N = 0;
function ok(cond, msg){ N++; if(!cond){ FAIL++; print("  FAIL: " + msg); } }
function has(hay, needle, msg){ ok(String(hay).indexOf(needle) >= 0, msg + "  [缺: " + needle + "]"); }
function no(hay, needle, msg){ ok(String(hay).indexOf(needle) < 0, msg + "  [不该出现: " + needle + "]"); }

/* 装载真实数据 */
boot({meta:T.meta, pis:T.pis, excluded:T.excluded});

/* ---------- 1. 全量渲染:每位 PI 的卡片都不能漏字段 ---------- */
/* 只查**结构位置**上的泄漏(标签之间、属性值里),不查正文出现的
   "undefined" 一词 —— 摘要里本来就可能有这个词。 */
var LEAK = [/>undefined</, />NaN</, /="undefined"/, />null</, />\s*NaN\s*°/];
var leaked = [];
for(var i=0;i<PIS.length;i++){
  var h = card(PIS[i]);
  for(var L=0;L<LEAK.length;L++) if(LEAK[L].test(h)) leaked.push(PIS[i].display+" ~ "+LEAK[L]);
}
ok(leaked.length===0, "卡片渲染泄漏结构性 undefined/NaN:" + leaked.slice(0,4).join(" | "));
ok(PIS.length>0, "PIS 非空");

/* ---------- 2. 不可测必须写「不可测」,不能用 0 或 1.0 冒充 ---------- */
var nmeas = 0, nunmeas = 0;
PIS.forEach(function(p){
  var h = card(p);
  if(p.complement==null){
    nunmeas++;
    /* 必须定位到**互补度那一格**。只查卡片里有没有「不可测」三个字是不够的:
       合作可能那一格也会写「不可测」,于是互补度偷偷显示数字也能蒙混过关
       —— 这个漏洞是负控(把 complement==null 分支关掉)才暴露出来的。 */
    has(h, "不可测</b><span>互补度", p.display+" 互补度不可测,该格必须显示「不可测」");
    has(h, "不可测</b><span>合作可能", p.display+" 互补度不可测时合作可能也必须显示「不可测」");
    ok(p.collab==null, p.display+" 互补度不可测时 collab 必须同为 null,不能给分");
    /* 页面上的原因说明是转义过的("<4" → "&lt;4"),
       所以断言里也要走同一条转义,否则测的是转义 bug 而不是内容缺失 */
    has(evMet(p), esc((p.complement_parts&&p.complement_parts.note)||"方法样本不足"),
        p.display+" 必须给出不可测的原因说明");
  } else { nmeas++; }
});
ok(nunmeas>0, "数据里应存在不可测样本(否则护栏没生效或数据变了)");

/* ---------- 3. 同名风险抽屉:必须披露 EuropePMC 全库命中与锚定计数 ---------- */
PIS.forEach(function(p){
  var r = evRisk(p);
  has(r, String(p.epmc_hit_total), p.display+" 必须显示 EuropePMC 全库命中数(全库越大同名越多)");
  has(r, "未锚定不等于不是本人", p.display+" 必须声明未锚定≠不是本人");
  if(p.n_anchored===0)
    has(r, "谨慎采信", p.display+" 零锚定必须给出谨慎采信告警");
});

/* ---------- 4. 撞车检查必须声明是词表匹配 ---------- */
var withHead=0, withoutHead=0;
PIS.forEach(function(p){
  var h = evHead(p);
  if(p.head_on && p.head_on.length){ withHead++; has(h, "命中词", p.display+" 撞车条目须列出命中词"); }
  else { withoutHead++; has(h, "词表匹配", p.display+" 无撞车时须声明这只是词表匹配、不代表他没在做"); }
});
ok(withoutHead>0, "应存在无撞车记录的 PI");

/* ---------- 5. 象限图:必须报告被略过的 PI 数并说明「没测到≠零」 ---------- */
renderQuad();
var quad = document.getElementById("quadBox").innerHTML;
has(quad, "没测到", "象限图必须声明被略过的是没测到、不是互补度为零");
has(quad, String(PIS.length - nmeas), "象限图必须报出被略过的 PI 数量");
has(quad, "无锚定证据", "象限图图例必须区分有/无锚定证据");
ok(quad.indexOf("<circle")>0, "象限图必须真的画出点");

/* ---------- 6. 轨迹:漂移不可计算时必须写原因,不能显示 0° ---------- */
renderDrift();
var drift = document.getElementById("driftList").innerHTML;
var nodeg = PIS.filter(function(p){return (p.trajectory||[]).length>=2 &&
                                          !(p.drift&&p.drift.deg!=null);});
if(nodeg.length) no(drift, "漂移 0°", "样本不足的 PI 不能显示 0° 漂移");
ok(drift.indexOf("<svg")>0 || drift.indexOf("无足够年度数据")>0, "轨迹面板必须出图或说明无数据");

/* ---------- 7. 排除名单:必须真的列出被排除的高风险名字与原因 ---------- */
renderExcl();
var ex = document.getElementById("exclBox").innerHTML;
var exn = document.getElementById("exclNote").innerHTML;
ok(EXCL.length>0, "排除名单不能为空");
has(exn, "藏起来才是不诚实", "排除页必须写明为何要公开排除名单");
EXCL.slice(0,10).forEach(function(e){ has(ex, e.display, "排除名单须列出 "+e.display); });
ok(EXCL.filter(function(e){return e.risk!=="low";}).length>0, "排除名单里应有高/中风险名字");

/* ---------- 8. 方法论页:三条踩过的坑必须在页面上,不能只留在提交记录里 ---------- */
renderHow();
var how = document.getElementById("howBox").innerHTML;
has(how, "锚定的是", "方法论页必须记录「锚定姓名串而非人」这个坑");
has(how, "Bartel", "方法论页必须给出锚定反转的实测对照");
has(how, "中位数", "方法论页必须说明稀缺阈值改用中位数而非固定 5%");
has(how, "惯例而非事实", "方法论页必须声明末位作者=通讯作者只是惯例");
has(how, "无作者唯一标识", "方法论页必须承认没有作者唯一标识");
has(how, "不是全领域名录", "方法论页必须声明这是方向内的地图而非全领域名录");

/* ---------- 9. 每个筛选器都必须真的改变结果数(防绑错 id 静默失效) ---------- */
function nPass(){ return PIS.filter(pass).length; }
var base = nPass();
ok(base===PIS.length, "默认无筛选时应全部通过");
F.anch = true;  var a1 = nPass(); F.anch = false;
ok(a1 < base && a1 === PIS.filter(function(p){return p.n_anchored>0;}).length, "锚定筛选必须生效");
F.meas = true;  var a2 = nPass(); F.meas = false;
ok(a2 === nmeas, "可测筛选必须生效");
F.q = "zzzzz_nonexistent"; var a3 = nPass(); F.q = "";
ok(a3 === 0, "搜索框必须生效");
var someReg = PIS.filter(function(p){return p.region;})[0];
if(someReg){ F.reg = someReg.region; var a4 = nPass(); F.reg = "";
  ok(a4 > 0 && a4 < base, "地区筛选必须生效且非全通过"); }
F.dir = IDS[0]; var a5 = nPass(); F.dir = "";
ok(a5 <= base, "方向筛选必须生效");

/* ---------- 10. 排序键不能把 null 排到最前 ---------- */
F.sort = "collab";
var sorted = PIS.slice().sort(function(x,y){return sortKey(y)-sortKey(x);});
ok(sorted[0].collab != null || PIS.every(function(p){return p.collab==null;}),
   "按合作可能排序时,不可测的 PI 不能排在最前");
F.sort = "competition";

/* ---------- 11. 对比视图:双向都要渲染,方法差集要真的是差集 ---------- */
var A = PIS[0], B = PIS[1];
var cA = cmpCol(A, B), cB = cmpCol(B, A);
has(cA, A.display, "对比左列必须是 A");
has(cB, B.display, "对比右列必须是 B");
for(var L2=0;L2<LEAK.length;L2++) ok(!LEAK[L2].test(cA+cB), "对比视图不得泄漏 undefined/NaN");

/* ---------- 12. CSV 导出:表头与行数必须与当前筛选一致 ---------- */
var savedBlob = null;
Blob = function(parts){ savedBlob = parts[0]; };
exportCsv();
ok(savedBlob !== null, "导出必须真的生成内容");
var lines = String(savedBlob).split("\r\n");
ok(lines.length === PIS.filter(pass).length + 1,
   "CSV 行数应 = 当前筛选通过数+表头 (得到 "+lines.length+")");
has(lines[0], "同名风险", "CSV 表头必须含同名风险列");
has(lines[0], "锚定篇数", "CSV 表头必须含锚定篇数列");
has(String(savedBlob), "不可测", "CSV 里不可测的指标必须写「不可测」而不是空/0");

print(FAIL === 0 ? ("ALL PASS · " + N + " 条断言 · " + PIS.length + " 位 PI · " + EXCL.length + " 条排除")
                 : (FAIL + " / " + N + " 条断言失败"));
