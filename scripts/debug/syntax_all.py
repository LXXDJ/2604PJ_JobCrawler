"""핵심 파일 syntax 점검."""
import ast

for f in [
    "crawlers/batch/runner.py",
    "crawlers/batch/list_crawler.py",
    "crawlers/batch/scheduler.py",
    "crawlers/extractors/list_extractor.py",
    "crawlers/registration/menu_validator.py",
    "dashboard/app.py",
    "scripts/ops/run_batch.py",
]:
    ast.parse(open(f, encoding="utf-8").read())
    print(f"{f}: OK")
