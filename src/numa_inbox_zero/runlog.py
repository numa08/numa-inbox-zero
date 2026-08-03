"""JSONL ログ追記。

runs.jsonl（実行単位）と decisions.jsonl（メール単位）は追記専用。
後から DuckDB / pandas で直接読める形式を保つ。
"""

from __future__ import annotations

import json
from pathlib import Path


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_decision_record(
    *,
    run_id: str,
    account: str,
    record,
    decision: dict | None,
    message: dict | None,
    log_subjects: bool,
) -> dict:
    """decisions.jsonl の 1 行を組み立てる。

    メールアドレス全体は残さずドメインのみ。件名は分類精度の検証に
    必要なので既定で記録するが、設定でオフにできる。
    """
    from .mail import from_domain

    entry: dict = {
        "run_id": run_id,
        "account": account,
        "message_id": record.message_id,
        "action": record.action,
        "applied": record.applied,
        "partial_reason": record.partial_reason,
    }
    if message is not None:
        entry["thread_id"] = message.get("thread_id")
        entry["from_domain"] = from_domain(message.get("from", ""))
        if log_subjects:
            entry["subject"] = message.get("subject", "")
        entry["body_chars"] = len(message.get("body", ""))
    if decision is not None:
        entry["reason"] = decision.get("reason", "")
        if decision.get("confidence"):
            entry["confidence"] = decision["confidence"]
        # 降格されたものは元の判定を残し、分類器のチューニング材料にする
        if decision.get("downgraded_from"):
            entry["downgraded_from"] = decision["downgraded_from"]
    if record.draft_id:
        entry["draft_id"] = record.draft_id
    return entry
