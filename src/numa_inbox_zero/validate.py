"""分類結果の検証。

メール本文は信用できない外部入力であり、それを読んだ分類器の出力も
無条件には信用しない。ここでの検証が「本文テキストが操作対象を選ぶ」
経路を塞ぐ最後の砦になる（SPEC.md §5）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

VALID_ACTIONS = frozenset({"archive", "star", "reply"})

CONFIDENCE_HIGH = "high"


@dataclass
class Rejection:
    message_id: str
    reason: str


@dataclass
class ValidationResult:
    accepted: list[dict] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)


def validate_decisions(decisions: list[dict], allowed_ids: set[str]) -> ValidationResult:
    """分類器の decisions を検証し、実行してよいものだけを返す。

    - message_id が fetch 時に渡した ID 集合に含まれない → 拒否
    - action が enum 外 → 拒否
    - 同一 message_id の重複 → 2 件目以降を拒否
    - reply なのに draft_body が空 → 拒否（ドラフトを作れない）
    """
    result = ValidationResult()
    seen: set[str] = set()

    for decision in decisions:
        message_id = str(decision.get("message_id", ""))
        action = decision.get("action")

        if message_id not in allowed_ids:
            result.rejected.append(Rejection(message_id, "message_id が入力に存在しない"))
            continue
        if message_id in seen:
            result.rejected.append(Rejection(message_id, "message_id が重複"))
            continue
        if action not in VALID_ACTIONS:
            result.rejected.append(Rejection(message_id, f"不正な action: {action!r}"))
            continue
        if action == "reply" and not (decision.get("draft_body") or "").strip():
            result.rejected.append(Rejection(message_id, "reply なのに draft_body が空"))
            continue

        seen.add(message_id)
        result.accepted.append(decision)

    return result


def downgrade_low_confidence(decisions: list[dict]) -> tuple[list[dict], int]:
    """確信度の低い archive を star に降格する。

    archive は 3 アクション中で唯一「受信トレイから消えて目に入らなくなる」
    不可逆側の操作。分類器が confidence: high を明示したときだけ実行し、
    それ以外（low・欠損・不正値）は star に降格して人間の判断を仰ぐ。
    プロンプトの指示が無視されてもコード側で安全に倒れる、という二重化。

    star / reply の迷いはスター付き一覧（ピン止め）で人の目に触れるため降格しない。
    戻り値は (変換後の decisions, 降格した件数)。
    """
    transformed: list[dict] = []
    downgraded = 0
    for decision in decisions:
        if decision.get("action") == "archive" and decision.get("confidence") != CONFIDENCE_HIGH:
            transformed.append({**decision, "action": "star", "downgraded_from": "archive"})
            downgraded += 1
        else:
            transformed.append(decision)
    return transformed, downgraded
