"""핵심 파일 syntax 점검."""
import ast
files = [
    "crawlers/batch/list_crawler.py",
    "crawlers/batch/runner.py",
    "crawlers/registration/menu_discovery.py",
    "crawlers/registration/menu_validator.py",
    "scripts/ops/install_schtask.py",
]
for f in files:
    ast.parse(open(f, encoding="utf-8").read())
    print(f"{f}: OK")
