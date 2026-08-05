"""分類結果の Gmail への適用。

reply はラベル付与 → ドラフト作成の順で実行する。逆順だと、ドラフト作成後の
ラベル付与が失敗した場合に次回の実行で同じメールが再取得され、同一スレッドに
重複ドラフトが作られてしまう（SPEC.md §5）。
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
    applied: str  # "ok" | "partial" | "failed" | "rejected" | "dry-run"
    partial_reason: str | None = None
    draft_id: str | None = None


@dataclass
class ApplySummary:
    archived: int = 0
    starred: int = 0
    drafted: int = 0
    rejected: int = 0
    errors: int = 0
    records: list[ApplyRecord] = field(default_factory=list)


def apply_decisions(
    service,
    *,
    validation: ValidationResult,
    messages_by_id: dict[str, dict],
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

        record = _apply_one(
            service,
            decision=decision,
            message=messages_by_id[message_id],
            processed_label_id=processed_label_id,
        )
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
    elif action == "reply":
        summary.drafted += 1


def _apply_one(service, *, decision: dict, message: dict, processed_label_id: str) -> ApplyRecord:
    message_id = decision["message_id"]
    action = decision["action"]

    try:
        if action == "archive":
            gmail.modify_labels(service, message_id, add=[processed_label_id], remove=["INBOX"])
            return ApplyRecord(message_id, action, "ok")

        # star / reply もアーカイブする。スター付きメールは受信トレイになくても
        # 「ピン止め」（スター付き一覧）に表示され、人の対応待ちはそちらが担うため、
        # 受信トレイは常に空に保つ
        if action == "star":
            gmail.modify_labels(
                service, message_id, add=["STARRED", processed_label_id], remove=["INBOX"]
            )
            return ApplyRecord(message_id, action, "ok")

        # reply: ラベル先行（モジュール docstring 参照）
        gmail.modify_labels(
            service, message_id, add=["STARRED", processed_label_id], remove=["INBOX"]
        )
    except HttpError as e:
        return ApplyRecord(message_id, action, "failed", f"ラベル付与に失敗: {e}")

    try:
        draft_id = gmail.create_reply_draft(service, message, decision["draft_body"])
        return ApplyRecord(message_id, action, "ok", draft_id=draft_id)
    except (HttpError, ValueError) as e:
        return ApplyRecord(
            message_id, action, "partial", f"ラベルは付与済み、ドラフト作成に失敗: {e}"
        )
