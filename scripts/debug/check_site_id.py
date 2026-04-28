"""site_id 추출 검증."""
from crawlers.registration.site_id import extract_site_id

CASES = [
    ("http://www.hanin.or.kr", "hanin"),
    ("https://siemreap.korean.net", "siemreap"),
    ("https://www.ppomppu.co.kr/zboard/zboard.php?id=guin", "ppomppu"),
    ("https://www.radiokorea.com/community/jobs.php", "radiokorea"),
    ("https://www.jobkorea.co.kr/recruit/joblist", "jobkorea"),
    ("https://job.incruit.com/jobdb_list/searchjob.asp", "incruit"),
    ("https://www.alba.co.kr/job/Main", "alba"),
    ("https://www.peoplenjob.com/jobs", "peoplenjob"),
    ("https://job.career.co.kr/jobs/", "career"),
    ("https://eps.hrdkorea.or.kr/", "hrdkorea"),
]

if __name__ == "__main__":
    fail = 0
    for url, want in CASES:
        got = extract_site_id(url)
        ok = got == want
        if not ok:
            fail += 1
        marker = "OK  " if ok else "FAIL"
        print(f"{marker}  got={got!r:14}  want={want!r:14}  {url}")
    print(f"\n{len(CASES) - fail}/{len(CASES)} passed")
