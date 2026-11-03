"""Measure how open ST source on GitHub is distributed over dialects.

Two measurements, each recorded with its query, the date and the API's
``total_count``; no number is estimated or invented:

* code search (files per container format: extension or a marker string of
  the format). GitHub code search requires authentication; without
  ``GITHUB_TOKEN`` (or ``GH_TOKEN``) it is recorded as not measured.
* repository search by topic (repositories per dialect). It works without
  authentication. Topics are set by the repository owners, overlap between
  dialects and do not imply that a repository contains ST source, so these
  counts describe where the communities are, not how much ST code exists.
  GitHub's language statistics have no Structured Text language, so a
  ``language:`` query is not possible.

This is the only part of the intake that uses the network, and it is not part
of normalization.

    python3 -m compiler.intake.ecosystem --output artifacts/intake/ecosystem.json
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

QUERIES = [
    {"format": "TwinCAT 3 POU (.TcPOU)", "family": "codesys", "query": "extension:TcPOU"},
    {"format": "TwinCAT 3 DUT (.TcDUT)", "family": "codesys", "query": "extension:TcDUT"},
    {"format": "CODESYS 2.3 export (.EXP)", "family": "codesys", "query": "extension:EXP END_FUNCTION_BLOCK"},
    {"format": "CODESYS library (.library)", "family": "codesys", "query": "extension:library"},
    {"format": "PLCopen XML", "family": "codesys / iec", "query": "\"www.plcopen.org/xml/tc6\" extension:xml"},
    {"format": "Siemens SCL (.scl)", "family": "siemens-scl", "query": "extension:scl FUNCTION_BLOCK"},
    {"format": "SIMATIC AX (.st with NAMESPACE)", "family": "simatic-ax", "query": "extension:st NAMESPACE END_NAMESPACE"},
    {"format": "IEC ST (.st)", "family": "any", "query": "extension:st END_FUNCTION_BLOCK"},
    {"format": "Rockwell L5X (.L5X)", "family": "logix", "query": "extension:L5X"},
    {"format": "B&R Automation Studio (.fun)", "family": "br", "query": "extension:fun END_FUNCTION_BLOCK"},
    {"format": "PC WORX / MULTIPROG (.zwt)", "family": "pcworx", "query": "extension:zwt"},
]
TOPICS = [
    {"family": "codesys", "topics": ["codesys", "twincat", "twincat3", "beckhoff", "wago"]},
    {"family": "siemens-scl", "topics": ["tia-portal", "step7", "scl"]},
    {"family": "simatic-ax", "topics": ["simatic-ax"]},
    {"family": "iec", "topics": ["openplc", "matiec", "beremiz"]},
    {"family": "sysmac", "topics": ["sysmac", "omron", "keyence"]},
    {"family": "gxworks", "topics": ["mitsubishi-plc", "gx-works2", "gx-works3"]},
    {"family": "br", "topics": ["automation-studio"]},
    {"family": "pcworx", "topics": ["pcworx", "plcnext"]},
    {"family": "control-expert", "topics": ["control-expert", "unity-pro"]},
    {"family": "logix", "topics": ["studio5000", "rslogix"]},
    {"family": "any", "topics": ["structured-text", "iec-61131-3", "plc"]},
]
API = "https://api.github.com/search/code?q="
REPOSITORY_API = "https://api.github.com/search/repositories?q="


def _search(url: str, token: str | None) -> dict:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
            data = json.loads(response.read())
        return {"total_count": data.get("total_count"), "incomplete_results": data.get("incomplete_results")}
    except (urllib.error.URLError, TimeoutError) as error:
        return {"total_count": None, "error": f"HTTP {error.code}" if hasattr(error, "code") else str(error)}


def measure(token: str, pause: float = 7.0) -> list[dict]:
    results = []
    for item in QUERIES:
        results.append({**item, **_search(API + urllib.parse.quote(item["query"]) + "&per_page=1", token)})
        time.sleep(pause)  # code search allows about 10 requests per minute
    return results


def measure_topics(token: str | None, pause: float = 7.0) -> list[dict]:
    results = []
    for group in TOPICS:
        for topic in group["topics"]:
            query = f"topic:{topic}"
            results.append({"family": group["family"], "query": query,
                            **_search(REPOSITORY_API + urllib.parse.quote(query) + "&per_page=1", token)})
            time.sleep(pause)  # unauthenticated search allows 10 requests per minute
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    record = {
        "schema_version": "semantist.ecosystem/1.1.0",
        "date": datetime.date.today().isoformat(),
        "code_search": {"method": "GitHub REST code search, total_count per query (per_page=1)",
                        "queries": QUERIES},
        "repository_topics": {"method": "GitHub REST repository search by topic, total_count per topic "
                                        "(per_page=1); topics are owner-assigned and overlap",
                              "results": measure_topics(token)},
    }
    if not token:
        record["code_search"].update({
            "status": "not_measured",
            "reason": "GitHub code search requires authentication (HTTP 401 without a token) and no "
                      "GITHUB_TOKEN/GH_TOKEN is available; nothing was measured or estimated"})
    else:
        record["code_search"].update({"status": "measured", "results": measure(token)})
    failed = [r["query"] for r in record["repository_topics"]["results"] if r["total_count"] is None]
    record["repository_topics"]["status"] = "measured" if not failed else "partial"
    record["status"] = (record["code_search"]["status"] + " (code search), "
                        + record["repository_topics"]["status"] + " (repository topics)")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(record["status"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
