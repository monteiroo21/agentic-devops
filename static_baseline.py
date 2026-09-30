import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from web_analyzer import Report, build_findings, check_urls, extract_links, record_result


def main():
    url = input("\nEnter the URL to analyze: ").strip()

    # What the tools found
    link_info = {}
    checks = {}

    # Metrics for the report
    started_at = datetime.now(timezone.utc).isoformat()
    start = time.perf_counter()

    # The same steps the agent is told to follow: extract the links, then check all of them
    page = extract_links(url)
    if page["error"]:
        print(f"=====Could not read the page: {page['error']}=====")
    record_result("extract_links", page, link_info, checks)
    record_result("check_urls", check_urls([link["url"] for link in page["links"]]), link_info, checks)

    report = Report(
        target=url,
        model="static",
        started_at=started_at,
        duration_s=round(time.perf_counter() - start, 1),
        llm_requests=0,
        input_tokens=0,
        output_tokens=0,
        counts=dict(Counter(check["verdict"] for check in checks.values())),
        narrative=None,
        findings=build_findings(checks, link_info, []),
    )
    report_json = report.model_dump_json(indent=2)
    print(report_json)
    Path("report_static.json").write_text(report_json)
    print("=====Report saved to report_static.json=====")


if __name__ == "__main__":
    main()
