#!/usr/bin/env python3
"""从 pytest-cov 的 coverage.json 生成 shields.io endpoint badge JSON。

用法：python scripts/gen_coverage_badge.py [coverage.json] [输出路径]
默认读 /tmp/cov.json，写 badges/coverage.json。供 CI 调用，保持工作流简单。
"""
import json
import os
import sys

src = sys.argv[1] if len(sys.argv) > 1 else "/tmp/cov.json"
dst = sys.argv[2] if len(sys.argv) > 2 else "badges/coverage.json"

with open(src, encoding="utf-8") as f:
    cov = json.load(f)["totals"]["percent_covered"]

color = (
    "brightgreen" if cov >= 90
    else "green" if cov >= 75
    else "yellowgreen" if cov >= 60
    else "yellow" if cov >= 40
    else "red"
)
badge = {"schemaVersion": 1, "label": "coverage", "message": f"{cov:.1f}%", "color": color}

os.makedirs(os.path.dirname(dst), exist_ok=True)
with open(dst, "w", encoding="utf-8") as f:
    json.dump(badge, f)
print(f"coverage {cov:.1f}% -> {color}")
