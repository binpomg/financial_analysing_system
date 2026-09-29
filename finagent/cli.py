"""Command-line entry point for local and offline competition runs."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from finagent.contracts import BusinessLine, DataClass, RunStatus, TaskSpec
from finagent.catalog import ingest_catalog, merge_catalogs, validate_catalog
from finagent.pilot import pilot_readiness, write_label_templates
from finagent.review import apply_review_bundle
from finagent.resolution import apply_fact_resolution
from finagent.ocr import process_ocr_job
from finagent.taxonomy import map_xbrl_document
from finagent.public_data import (
    CrawlLimits,
    CninfoCollectionRequest,
    CninfoCollector,
    DEFAULT_CNINFO_ISSUERS,
    SecCollectionRequest,
    SecEdgarCollector,
    dry_run_cninfo,
    dry_run_sec_edgar,
)
from finagent.corpus import SampleCorpus
from finagent.runtime import Runtime


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="finagent")
    command.add_argument(
        "--state-dir", type=Path, default=Path(__file__).resolve().parents[1] / "runtime" / "agent",
        help="local metadata and object store; defaults to the workbench runtime/agent"
    )
    subcommands = command.add_subparsers(dest="command", required=True)
    subcommands.add_parser("lines", help="list seven business lines")
    sample_tasks = subcommands.add_parser("sample-tasks", help="list delivered sample corpus tasks")
    sample_tasks.add_argument("--corpus", type=Path, required=True)
    catalog_validate = subcommands.add_parser("catalog-validate", help="validate a pilot source catalog")
    catalog_validate.add_argument("--catalog", type=Path, required=True)
    catalog_ingest = subcommands.add_parser("catalog-ingest", help="ingest a validated pilot source catalog")
    catalog_ingest.add_argument("--catalog", type=Path, required=True)
    catalog_merge = subcommands.add_parser(
        "catalog-merge", help="merge source catalogs into one non-copying pilot catalog"
    )
    catalog_merge.add_argument("--catalog", type=Path, action="append", required=True)
    catalog_merge.add_argument("--output", type=Path, required=True)
    catalog_merge.add_argument("--pilot-id", required=True)
    pilot_readiness_command = subcommands.add_parser(
        "pilot-readiness", help="report company-level trial coverage and split readiness"
    )
    pilot_readiness_command.add_argument("--catalog", type=Path, required=True)
    label_templates = subcommands.add_parser(
        "pilot-label-templates", help="write empty human-review packages for annual-report gold labels"
    )
    label_templates.add_argument("--catalog", type=Path, required=True)
    label_templates.add_argument("--output-dir", type=Path, required=True)
    collect_sec = subcommands.add_parser(
        "collect-sec-edgar",
        help="conservatively collect selected public SEC EDGAR filings; dry-run by default",
    )
    collect_sec.add_argument("--cik", required=True, help="SEC CIK, with or without leading zeroes")
    collect_sec.add_argument("--output-dir", type=Path, default=Path("public_data"))
    collect_sec.add_argument("--forms", default="10-K", help="comma-separated: 10-K,10-Q,20-F,40-F")
    collect_sec.add_argument("--max-filings", type=int, default=3)
    collect_sec.add_argument("--include-companyfacts", action="store_true")
    collect_sec.add_argument("--quota-gib", type=float, default=2.0, help="hard maximum is 2 GiB")
    collect_sec.add_argument("--max-file-mib", type=float, default=100.0)
    collect_sec.add_argument("--max-requests", type=int, default=50)
    collect_sec.add_argument("--rate-limit", type=float, default=5.0, help="requests/second; maximum 10")
    collect_sec.add_argument("--timeout", type=float, default=30.0)
    collect_sec.add_argument("--retries", type=int, default=2)
    collect_sec.add_argument("--user-agent", help="required with --execute; include a contact email")
    collect_sec.add_argument("--execute", action="store_true", help="allow network retrieval from SEC")
    collect_cninfo = subcommands.add_parser(
        "collect-cninfo-annual-reports",
        help="collect selected public CNINFO annual reports; dry-run by default",
    )
    collect_cninfo.add_argument("--output-dir", type=Path, default=Path("public_data/cninfo"))
    collect_cninfo.add_argument("--fiscal-years", default="2022,2023")
    collect_cninfo.add_argument("--quota-gib", type=float, default=2.0, help="hard maximum is 2 GiB")
    collect_cninfo.add_argument("--max-file-mib", type=float, default=100.0)
    collect_cninfo.add_argument("--max-requests", type=int, default=50)
    collect_cninfo.add_argument("--rate-limit", type=float, default=2.0, help="requests/second; maximum 10")
    collect_cninfo.add_argument("--timeout", type=float, default=30.0)
    collect_cninfo.add_argument("--retries", type=int, default=2)
    collect_cninfo.add_argument("--execute", action="store_true", help="allow network retrieval from CNINFO")
    candidates = subcommands.add_parser("candidates", help="show parser candidates for a document")
    candidates.add_argument("document_id")
    map_xbrl = subcommands.add_parser("map-xbrl", help="map an XML/XBRL document into review candidates")
    map_xbrl.add_argument("--document-id", required=True)
    map_xbrl.add_argument("--taxonomy-map", type=Path, required=True)
    apply_review = subcommands.add_parser("apply-review", help="convert approved candidates into verified facts")
    apply_review.add_argument("--review", type=Path, required=True)
    apply_resolution = subcommands.add_parser("apply-resolution", help="record a reviewer decision for conflicting facts")
    apply_resolution.add_argument("--resolution", type=Path, required=True)
    ocr_jobs = subcommands.add_parser("ocr-jobs", help="list OCR review jobs")
    ocr_jobs.add_argument("--status", choices=["queued", "processing", "completed", "failed"])
    process_ocr = subcommands.add_parser("process-ocr", help="run one queued OCR job with the local OCR engine")
    process_ocr.add_argument("job_id")
    process_ocr.add_argument("--language", default="chi_sim+eng")
    process_ocr.add_argument("--tesseract-cmd", type=Path)
    requeue_ocr = subcommands.add_parser("requeue-ocr", help="retry a failed OCR job after fixing its local dependency")
    requeue_ocr.add_argument("job_id")
    ocr_transcript = subcommands.add_parser("ocr-transcript", help="show immutable OCR transcripts for a job")
    ocr_transcript.add_argument("job_id")
    fact_conflicts = subcommands.add_parser("fact-conflicts", help="show conflicting verified facts")
    fact_conflicts.add_argument("--entity-id", required=True)
    fact_conflicts.add_argument("--as-of", required=True)
    run_task = subcommands.add_parser("run-task", help="run a delivered sample corpus task")
    run_task.add_argument("--corpus", type=Path, required=True)
    run_task.add_argument("task_id")
    evaluate = subcommands.add_parser("evaluate", help="evaluate a sample run against independent references")
    evaluate.add_argument("--corpus", type=Path, required=True)
    evaluate.add_argument("run_id")
    run = subcommands.add_parser("run", help="run a business line")
    run.add_argument("line", type=int, choices=range(1, 8))
    run.add_argument("sources", nargs="+", help="file paths or existing document IDs")
    run.add_argument("--as-of", required=True, type=date.fromisoformat)
    run.add_argument("--entity-id")
    run.add_argument("--question", default="")
    run.add_argument("--max-model-calls", type=int, default=2)
    run.add_argument("--public", action="store_true", help="source is licensed for external model use")
    show = subcommands.add_parser("show", help="show a run and its artifacts")
    show.add_argument("run_id")
    resume = subcommands.add_parser("resume", help="resume an interrupted durable workflow run")
    resume.add_argument("run_id")
    resume.add_argument("--corpus", type=Path, help="required when resuming a sample corpus task")
    return command


def main(argv: list[str] | None = None) -> int:
    command = parser()
    args = command.parse_args(argv)
    if args.command == "lines":
        print(json.dumps({line.value: line.name.lower() for line in BusinessLine}, indent=2))
        return 0
    if args.command == "sample-tasks":
        corpus = SampleCorpus(args.corpus)
        print(
            json.dumps(
                [
                    {
                        "task_id": item.task_id,
                        "line": item.line.value,
                        "title": item.title,
                        "as_of": item.as_of.isoformat(),
                        "expected_output_fields": item.expected_output_fields,
                    }
                    for item in corpus.tasks()
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "collect-sec-edgar":
        request = SecCollectionRequest(
            cik=args.cik,
            forms=tuple(item for item in args.forms.split(",") if item.strip()),
            max_filings=args.max_filings,
            include_companyfacts=args.include_companyfacts,
        )
        limits = CrawlLimits(
            quota_bytes=int(args.quota_gib * 1024**3),
            max_file_bytes=int(args.max_file_mib * 1024**2),
            max_requests=args.max_requests,
            rate_limit_per_second=args.rate_limit,
            timeout_seconds=args.timeout,
            retries=args.retries,
        )
        if not args.execute:
            print(json.dumps(dry_run_sec_edgar(request, args.output_dir, limits), ensure_ascii=False, indent=2))
            return 0
        if not args.user_agent:
            command.error("--execute requires --user-agent with a contact email")
        result = SecEdgarCollector(limits, args.user_agent).collect(request, args.output_dir)
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return 0 if not result.issues else 2
    if args.command == "collect-cninfo-annual-reports":
        try:
            fiscal_years = tuple(int(item.strip()) for item in args.fiscal_years.split(",") if item.strip())
        except ValueError:
            command.error("--fiscal-years must be comma-separated calendar years")
        request = CninfoCollectionRequest(
            issuers=DEFAULT_CNINFO_ISSUERS,
            fiscal_years=fiscal_years,
        )
        limits = CrawlLimits(
            quota_bytes=int(args.quota_gib * 1024**3),
            max_file_bytes=int(args.max_file_mib * 1024**2),
            max_requests=args.max_requests,
            rate_limit_per_second=args.rate_limit,
            timeout_seconds=args.timeout,
            retries=args.retries,
        )
        if not args.execute:
            print(json.dumps(dry_run_cninfo(request, args.output_dir, limits), ensure_ascii=False, indent=2))
            return 0
        result = CninfoCollector(limits).collect(request, args.output_dir)
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return 0 if not result.issues else 2
    runtime = Runtime(args.state_dir)
    if args.command == "pilot-readiness":
        print(json.dumps(pilot_readiness(args.catalog), ensure_ascii=False, indent=2))
        return 0
    if args.command == "pilot-label-templates":
        created = write_label_templates(args.catalog, args.output_dir)
        print(json.dumps({"created": [str(item) for item in created]}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "catalog-validate":
        validation = validate_catalog(args.catalog)
        print(json.dumps(validation, ensure_ascii=False, indent=2))
        return 0 if validation["valid"] else 2
    if args.command == "catalog-ingest":
        result = ingest_catalog(runtime.store, args.catalog)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["valid"] else 2
    if args.command == "catalog-merge":
        result = merge_catalogs(args.catalog, args.output, args.pilot_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["valid"] else 2
    if args.command == "candidates":
        document = runtime.store.get_document(args.document_id)
        if document is None:
            command.error(f"unknown document ID: {args.document_id}")
        print(
            json.dumps(
                [item.model_dump(mode="json") for item in runtime.store.candidates_for_document(document.id)],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "map-xbrl":
        result = map_xbrl_document(runtime.store, args.document_id, args.taxonomy_map)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "apply-review":
        result = apply_review_bundle(runtime.store, args.review)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "apply-resolution":
        result = apply_fact_resolution(runtime.store, args.resolution)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "ocr-jobs":
        print(
            json.dumps(
                [item.model_dump(mode="json") for item in runtime.store.ocr_jobs(args.status)],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "process-ocr":
        result = process_ocr_job(
            runtime.store,
            args.job_id,
            language=args.language,
            tesseract_executable=(str(args.tesseract_cmd) if args.tesseract_cmd else None),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "completed" else 2
    if args.command == "requeue-ocr":
        job = runtime.store.requeue_ocr_job(args.job_id)
        print(json.dumps(job.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return 0
    if args.command == "ocr-transcript":
        job = runtime.store.ocr_job_by_id(args.job_id)
        if job is None:
            command.error(f"unknown OCR job ID: {args.job_id}")
        print(
            json.dumps(
                [item.model_dump(mode="json") for item in runtime.store.ocr_transcripts_for_job(job.id)],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "fact-conflicts":
        print(
            json.dumps(
                runtime.store.fact_conflicts(args.entity_id, args.as_of),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "show":
        run = runtime.store.get_run(args.run_id)
        if run is None:
            parser().error(f"unknown run ID: {args.run_id}")
        print(
            json.dumps(
                {
                    "run": run.model_dump(mode="json"),
                    "events": runtime.store.events_for(run.id),
                    "artifacts": runtime.store.artifacts_for(run.id),
                    "workflow_nodes": [
                        item.model_dump(mode="json") for item in runtime.store.workflow_nodes_for(run.id)
                    ],
                    "review_findings": [
                        item.model_dump(mode="json") for item in runtime.store.review_findings_for(run.id)
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "resume":
        try:
            run = runtime.resume(args.run_id, args.corpus)
        except (KeyError, ValueError) as exc:
            command.error(str(exc))
        print(
            json.dumps(
                {
                    "run": run.model_dump(mode="json"),
                    "workflow_nodes": [
                        item.model_dump(mode="json") for item in runtime.store.workflow_nodes_for(run.id)
                    ],
                    "review_findings": [
                        item.model_dump(mode="json") for item in runtime.store.review_findings_for(run.id)
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2 if run.status in {RunStatus.FAILED, RunStatus.BLOCKED} else 0
    if args.command == "run-task":
        run = runtime.run_sample_task(args.corpus, args.task_id)
        print(
            json.dumps(
                {"run": run.model_dump(mode="json"), "artifacts": runtime.store.artifacts_for(run.id)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2 if run.status in {RunStatus.FAILED, RunStatus.BLOCKED} else 0
    if args.command == "evaluate":
        evaluation = runtime.evaluate_sample_run(args.corpus, args.run_id)
        print(json.dumps(evaluation, ensure_ascii=False, indent=2))
        return 0 if evaluation["status"] == "passed" else 2
    task = TaskSpec(
        line=BusinessLine(args.line),
        sources=args.sources,
        entity_id=args.entity_id,
        as_of=args.as_of,
        question=args.question,
        max_model_calls=args.max_model_calls,
        source_data_class=DataClass.PUBLIC if args.public else DataClass.RESTRICTED,
    )
    run = runtime.run(task)
    result = runtime.store.artifacts_for(run.id)
    print(
        json.dumps(
            {"run": run.model_dump(mode="json"), "artifacts": result},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 2 if run.status in {RunStatus.FAILED, RunStatus.BLOCKED} else 0


if __name__ == "__main__":
    raise SystemExit(main())
