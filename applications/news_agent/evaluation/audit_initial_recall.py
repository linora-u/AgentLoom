"""List every initially rejected record and flag likely material facts for recall audit.

The flags only order manual review. They are not a news filter or a claim that
unflagged records cannot contain missed ETF opportunities.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .baseline import verify_evaluation_artifacts
from ..processing.prepare import _atomic_text


FACT_TERMS = re.compile(
    r"业绩预告|扣非|净利润|订单|中标|签约|核准|获批|通过|正式实施|生效|补贴|"
    r"专项资金|增产|减产|停产|复产|产能|投产|交付|集采|关税|许可证|"
    r"并购|收购|出口|进口|制裁|停火|断供|配额|储备|指导价|降价|提价"
)
AMOUNT = re.compile(r"\d[\d,.]*\s*(?:万亿|亿元|万元|万吨|万台|GW|MW|亿|万|台|%)")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(root: Path, day: date) -> dict[str, Any]:
    verify_evaluation_artifacts(root, day)
    stem = day.isoformat()
    folder = root / "days" / stem
    manifest_path = folder / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    chosen: set[str] = set()
    initial_hashes: dict[str, str] = {}
    for batch in manifest["batches"]:
        file = root / "results" / "initial" / stem / batch["file"].replace(".txt", ".json")
        answer = json.loads(file.read_text(encoding="utf-8"))
        ids = answer["selected_ids"]
        if not isinstance(ids, list) or len(ids) != len(set(ids)) or not set(ids) <= set(batch["ids"]):
            raise ValueError(f"{stem}: invalid initial result for {batch['file']}")
        chosen.update(ids)
        initial_hashes[batch["file"]] = _sha(file)

    rejected: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for line in (folder / "records.jsonl").open(encoding="utf-8"):
        record = json.loads(line)
        counts["prepared"] += 1
        if record["record_id"] in chosen:
            counts["selected"] += 1
            continue
        counts["rejected"] += 1
        title = record.get("title") or ""
        # Search the full body as well: a neutral title can conceal a concrete fact.
        full_text = title + "\n" + (record.get("text") or "")
        terms = sorted(set(FACT_TERMS.findall(full_text)))
        amounts = AMOUNT.findall(full_text)
        flagged = bool(terms and amounts)
        if flagged:
            counts["flagged_for_review"] += 1
        rejected.append({"record_id": record["record_id"],
                         "category": record["category"],
                         "published_at": record.get("published_at"),
                         "title": title,
                         "body_status": record.get("body_status"),
                         "flagged_for_review": flagged,
                         "fact_terms": terms,
                         "amount_examples": amounts[:5],
                         "text_sha256": hashlib.sha256(
                             (record.get("text") or "").encode("utf-8")).hexdigest()})
    if counts["prepared"] != manifest["prepared_count"]:
        raise ValueError(f"{stem}: prepared count mismatch")
    if counts["selected"] != len(chosen):
        raise ValueError(f"{stem}: initial IDs absent from records")
    return {"day": stem, "records_sha256": manifest["records_sha256"],
            "manifest_sha256": _sha(manifest_path),
            "initial_result_hashes": initial_hashes,
            "counts": dict(counts),
            "audit_state": "retrieval_leads_only_all_rejected_records_listed",
            "rejected_records": rejected}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--day", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    value = build(args.input_root, args.day)
    target = args.input_root / "reports" / "initial_recall" / f"{args.day}.json"
    _atomic_text(target, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(target), **value["counts"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
