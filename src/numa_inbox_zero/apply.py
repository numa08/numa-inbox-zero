"""分類結果の Gmail への適用。

archive も star も「受信トレイから外す + 処理済みラベルを付ける」だけの
ラベル操作に閉じている。メールの送信・下書き作成は行わない（SPEC.md §5）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from googleapiclient.errors import HttpError

from . import gmail
from .validate import ValidationResult


@dataclass
class ApplyRecord:
    message_id: str
    action: str
    applied: str  # "ok" | "failed" | "rejected" | "dry-run"
    failure_reason: str | None = None


@dataclass
class ApplySummary:
    archived: int = 0
    starred: int = 0
    rejected: int = 0
    errors: int = 0
    records: list[ApplyRecord] = field(default_factory=list)


def apply_decisions(
    service,
    *,
    validation: ValidationResult,
    processed_label_id: str,
    dry_run: bool,
) -> ApplySummary:
    summary = ApplySummary()

    for rejection in validation.rejected:
        summary.rejected += 1
        summary.records.append(
            ApplyRecord(rejection.message_id, "unknown", "rejected", rejection.reason)
        )

    for decision in validation.accepted:
        message_id = decision["message_id"]
        action = decision["action"]

        if dry_run:
            summary.records.append(ApplyRecord(message_id, action, "dry-run"))
            _count_action(summary, action)
            continue

        record = _apply_one(service, decision=decision, processed_label_id=processed_label_id)
        summary.records.append(record)
        if record.applied == "ok":
            _count_action(summary, action)
        else:
            summary.errors += 1

    return summary


def _count_action(summary: ApplySummary, action: str) -> None:
    if action == "archive":
        summary.archived += 1
    elif action == "star":
        summary.starred += 1


def _apply_one(service, *, decision: dict, processed_label_id: str) -> ApplyRecord:
    message_id = decision["message_id"]
    action = decision["action"]

    # star もアーカイブする。スター付きメールは受信トレイになくても
    # 「ピン止め」（スター付き一覧）に表示され、人の対応待ちはそちらが担うため、
    # 受信トレイは常に空に保つ。
    add = ["STARRED", processed_label_id] if action == "star" else [processed_label_id]

    try:
        gmail.modify_labels(service, message_id, add=add, remove=["INBOX"])
    except HttpError as e:
        return ApplyRecord(message_id, action, "failed", f"ラベル操作に失敗: {e}")
    return ApplyRecord(message_id, action, "ok")
