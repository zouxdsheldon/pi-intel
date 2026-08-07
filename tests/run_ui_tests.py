#!/usr/bin/env python3
"""tests/run_ui_tests.py —— PI 情报面板前端行为测试(离线,无需浏览器)

做法:
  1. 把 index.html 最后一个 <script> 块抽出来,去掉末尾的 fetch() 引导调用,
     只留函数定义;
  2. 用一个极简 DOM 桩(tests/harness.js)喂给它;
  3. 用 data/pis.json 的**全部**真实记录跑每个渲染函数并断言。

断言的是「诚实性契约」而不只是「不报错」——见 tests/assertions.js:
  · 互补度/合作可能不可测时必须显示「不可测」,不能用 0 或 1.0 冒充
  · 同名风险抽屉必须披露 Europe PMC 全库命中数与锚定计数
  · 零锚定的 PI 必须给出「指纹请谨慎采信」告警
  · 象限图必须报出被略过的 PI 数并声明「没测到 ≠ 互补度为零」
  · 排除名单必须真的列出高风险名字与排除原因
  · 方法论页必须记录锚定反转、阈值改中位数、末位作者只是惯例这三条
  · 每个筛选器都必须真的改变通过数(防止绑错 id 后静默失效)
  · CSV 行数必须与当前筛选一致

跑法:  python3 tests/run_ui_tests.py
引擎:  macOS 自带 JavaScriptCore(jsc);Linux 上自动退回 node。
"""
import json, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSC = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"
ENGINE = JSC if os.path.exists(JSC) else "node"


def build():
    s = open(os.path.join(ROOT, "index.html")).read()
    blk = re.findall(r"<script[^>]*>(.*?)</script>", s, re.S)[-1]
    cut = blk.rindex('fetch("data/pis.json')          # 去掉引导调用,只留函数定义
    core = blk[:cut]
    d = json.load(open(os.path.join(ROOT, "data/pis.json")))
    fx = os.path.join(ROOT, "tests/_fixture.json")
    json.dump(d, open(fx, "w"), ensure_ascii=False)
    harness = open(os.path.join(ROOT, "tests/harness.js")).read().replace("__FIXTURE__", fx)
    asserts = open(os.path.join(ROOT, "tests/assertions.js")).read()
    out = os.path.join(ROOT, "tests/_bundle.js")
    open(out, "w").write(harness + core + asserts)
    return out, len(d["pis"]), len(d.get("excluded", []))


def main():
    bundle, n, nx = build()
    r = subprocess.run([ENGINE, bundle], capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip())
    ok = "ALL PASS" in r.stdout
    print(f"[{'PASS' if ok else 'FAIL'}] {n} 位 PI · {nx} 条排除 · engine={os.path.basename(ENGINE)}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
