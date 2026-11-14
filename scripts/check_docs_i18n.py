#!/usr/bin/env python3
"""Check that English docs stay aligned with their Chinese sources (structure + links)."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAIRS = [
    ("docs/功能介绍.md", "docs/en/features.md"),
    ("docs/使用说明.md", "docs/en/usage.md"),
    ("docs/收费使用说明.md", "docs/en/billing.md"),
    ("docs/收费升级报告.md", "docs/en/billing-report.md"),
    ("docs/部署与维护.md", "docs/en/deployment.md"),
    ("docs/测试与验收报告.md", "docs/en/acceptance-report.md"),
    ("CONTRACT.md", "CONTRACT.en.md"),
    ("SECURITY.md", "SECURITY.en.md"),
]

CJK = re.compile(r"[\u4e00-\u9fff]")


def strip_code(text: str) -> str:
    return re.sub(r"```.*?```", "", text, flags=re.S)


def counts(text: str) -> dict:
    body = strip_code(text)
    return {
        "h2": len(re.findall(r"^## ", body, flags=re.M)),
        "table_rows": len([l for l in body.splitlines() if l.strip().startswith("|")]),
        "bullets": len([l for l in body.splitlines() if re.match(r"^\s*[-*] ", l)]),
        "lines": len(text.splitlines()),
    }


def main() -> int:
    problems = 0
    for zh, en in PAIRS:
        zh_p, en_p = ROOT / zh, ROOT / en
        if not en_p.exists():
            print(f"MISSING  {en}")
            problems += 1
            continue
        a, b = zh_p.read_text(encoding="utf-8"), en_p.read_text(encoding="utf-8")
        ca, cb = counts(a), counts(b)
        ratio = cb["lines"] / max(ca["lines"], 1)
        bad = []
        if abs(cb["h2"] - ca["h2"]) > 2:
            bad.append(f"h2 {ca['h2']}->{cb['h2']}")
        if abs(cb["table_rows"] - ca["table_rows"]) > max(3, ca["table_rows"] * 0.2):
            bad.append(f"rows {ca['table_rows']}->{cb['table_rows']}")
        if not 0.6 <= ratio <= 1.5:
            bad.append(f"lines {ca['lines']}->{cb['lines']} ({ratio:.2f})")
        cjk_lines = [l for l in en_p.read_text(encoding="utf-8").splitlines() if CJK.search(l)]
        # allow the language switcher line and a few quoted originals
        cjk_lines = [l for l in cjk_lines if "中文" not in l]
        if len(cjk_lines) > 6:
            bad.append(f"CJK lines {len(cjk_lines)}")
        status = "OK  " if not bad else "WARN"
        if bad:
            problems += 1
        print(f"{status} {en:34} h2={cb['h2']:>2} rows={cb['table_rows']:>3} lines={cb['lines']:>4} ({ratio:.2f}) "
              + ("; ".join(bad) if bad else ""))
        if cjk_lines and len(cjk_lines) <= 6:
            for l in cjk_lines:
                print(f"       cjk: {l.strip()[:90]}")
    print(f"\n{problems} file(s) need attention")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
