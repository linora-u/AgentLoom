"""List selected news absent from refined candidate reviews, without calling them missed events."""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import unicodedata

from ..baseline import _checked_initial_ids
from ..processing.prepare import _atomic_text
from .baseline import verify_evaluation_artifacts


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _title_key(value: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", value).casefold()
                   if char.isalnum())


def build(root: Path, day: date) -> dict:
    verify_evaluation_artifacts(root, day)
    stem = day.isoformat()
    folder = root / "days" / stem
    manifest_path = folder / "manifest.json"
    records_path = folder / "records.jsonl"
    reviews_path = root / "results" / "reviews" / f"{stem}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = {record["record_id"]: record
               for line in records_path.open(encoding="utf-8")
               if (record := json.loads(line))}
    selected: list[str] = []
    initial_hashes: dict[str, str] = {}
    for batch in manifest["batches"]:
        path = root / "results" / "initial" / stem / batch["file"].replace(".txt", ".json")
        selected.extend(_checked_initial_ids(json.loads(path.read_text(encoding="utf-8")), batch))
        initial_hashes[batch["file"]] = _sha(path)
    if len(selected) != len(set(selected)) or not set(selected) <= set(records):
        raise ValueError(f"{stem}: selected ID coverage invalid")
    reviews = json.loads(reviews_path.read_text(encoding="utf-8"))["reviews"]
    covered = {record_id for review in reviews if review["stage"] == "chunk"
               for record_id in review["record_ids"]}
    if not covered <= set(selected):
        raise ValueError(f"{stem}: refined review cites unselected news")
    reviewed_titles: dict[str, list[str]] = {}
    reviewed_bodies: dict[str, list[str]] = {}
    for record_id in selected:
        if record_id not in covered:
            continue
        record = records[record_id]
        title_key = _title_key(record.get("title") or "")
        body = record.get("text") or ""
        if title_key:
            reviewed_titles.setdefault(title_key, []).append(record_id)
        if body:
            reviewed_bodies.setdefault(hashlib.sha256(body.encode("utf-8")).hexdigest(),
                                       []).append(record_id)
    unreviewed = []
    for record_id in selected:
        if record_id in covered:
            continue
        record = records[record_id]
        body_digest = hashlib.sha256((record.get("text") or "").encode("utf-8")).hexdigest()
        unreviewed.append({"record_id": record_id,
                           "published_at": record.get("published_at"),
                           "title": record.get("title") or "",
                           "category": record["category"],
                           "body_status": record.get("body_status"),
                           "text_sha256": body_digest,
                           "same_title_reviewed_ids": reviewed_titles.get(
                               _title_key(record.get("title") or ""), []),
                           "same_body_reviewed_ids": (reviewed_bodies.get(body_digest, [])
                                                      if record.get("text") else [])})
    return {"day": stem,
            "state": "selected_news_without_refined_review_needs_duplicate_and_fact_audit",
            "records_sha256": _sha(records_path),
            "manifest_sha256": _sha(manifest_path),
            "reviews_sha256": _sha(reviews_path),
            "initial_result_hashes": initial_hashes,
            "selected_news": len(selected),
            "selected_news_represented_in_chunk_reviews": len(covered),
            "selected_news_without_chunk_review": len(unreviewed),
            "unreviewed_with_exact_reviewed_title": sum(
                bool(row["same_title_reviewed_ids"]) for row in unreviewed),
            "unreviewed_with_exact_reviewed_body": sum(
                bool(row["same_body_reviewed_ids"]) for row in unreviewed),
            "qualification": "No review row is not a verified missed event; each record needs duplicate, source, fact, and ETF relevance review.",
            "unreviewed_records": unreviewed}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--day", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    result = build(args.input_root, args.day)
    path = args.input_root / "reports" / "refined_coverage" / f"{args.day}.json"
    _atomic_text(path, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(path), "selected": result["selected_news"],
                      "represented": result["selected_news_represented_in_chunk_reviews"],
                      "unreviewed": result["selected_news_without_chunk_review"],
                      "same_title_leads": result["unreviewed_with_exact_reviewed_title"],
                      "same_body_leads": result["unreviewed_with_exact_reviewed_body"]},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
