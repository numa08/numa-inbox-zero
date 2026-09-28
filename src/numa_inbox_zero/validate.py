"""分類結果の検証と archive ゲート。

メール本文は信用できない外部入力であり、それを読んだ分類器の出力も
無条件には信用しない。ここでの検証が「本文テキストが操作対象を選ぶ」
経路を塞ぐ最後の砦になる（SPEC.md §5）。

Jev は出力が型（policy のカテゴリ名）に閉じており、message_id も
コード側が持つため捏造の余地がない。それでも検証を残すのは、
policy とモデルの応答のずれ・実装ミス・将来のバックエンド差し替えで
不変条件が崩れないようにするため。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .policy import VALID_ACTIONS, Thresholds


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
    - action が enum 外（policy に無いカテゴリが選ばれた場合も None になる）→ 拒否
    - 同一 message_id の重複 → 2 件目以降を拒否
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
            result.rejected.append(
                Rejection(
                    message_id,
                    f"不正な action: {action!r}（category={decision.get('category')!r}）",
                )
            )
            continue

        seen.add(message_id)
        result.accepted.append(decision)

    return result


def gate_archive(decisions: list[dict], thresholds: Thresholds) -> tuple[list[dict], int]:
    """確信度の低い archive、インジェクションが疑われる archive を star に降格する。

    archive は 2 アクション中で唯一「受信トレイから消えて目に入らなくなる」
    不可逆側の操作。Jev の confidence は probabilities から導出された較正済みの
    値なので、閾値未満は人間の判断に回す。

    injection の Noul が高い（本文が処理系に指示を出そうとしている）メールも
    archive させない。Jev は state を敵対的入力として扱わないと明言されており
    （model-jaggedness/jev-1.13）、判定が本文に誘導されうる前提で、
    不可逆側の操作だけをコードで止める。

    star は受信トレイから外してもスター付き一覧（ピン止め）で人の目に触れるため、
    降格先として安全。戻り値は (変換後の decisions, 降格した件数)。
    """
    transformed: list[dict] = []
    downgraded = 0
    for decision in decisions:
        reason = _downgrade_reason(decision, thresholds)
        if reason is None:
            transformed.append(decision)
            continue
        transformed.append(
            {**decision, "action": "star", "downgraded_from": "archive", "downgrade_reason": reason}
        )
        downgraded += 1
    return transformed, downgraded


def _downgrade_reason(decision: dict, thresholds: Thresholds) -> str | None:
    """archive を止める理由。止めないなら None。

    confidence / injection が欠損・非数値の場合も降格する（安全側）。
    """
    if decision.get("action") != "archive":
        return None

    confidence = _as_probability(decision.get("confidence"))
    if confidence is None or confidence < thresholds.archive_confidence:
        return f"confidence={decision.get('confidence')!r} < {thresholds.archive_confidence}"

    injection = _as_probability(decision.get("injection"))
    if injection is None or injection > thresholds.injection:
        return f"injection={decision.get('injection')!r} > {thresholds.injection}"

    return None


def _as_probability(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not 0.0 <= value <= 1.0:
        return None
    return float(value)
