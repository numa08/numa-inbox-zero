"""オフライン評価（eval import / run / diff）の中核ロジック。

設計思想（SPEC.md / eval ワークフロー）:
- 正解ラベルは運用の副産物（inbox.json + classification.json）から昇格させる
- 1実験 = 1結果ファイル。プロンプトのハッシュを埋めて再現可能に保つ
- 合否は集計スコアでなく差分の中身で決める。そのため結果ファイルに
  per-message の予測を残し、diff で「判定が変わった件」を出せるようにする

誤りコストは非対称: false archive（重要メールの見逃し）が最も高い。
主要メトリクスは archive_precision と reply_recall の 2 つ。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .validate import VALID_ACTIONS

# 分類器が判定を返さなかったメールの擬似アクション。
# 見逃しも誤りとして混同行列に現れるようにする。
MISSING = "missing"


def load_golden(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}:{line_no} が JSON としてパースできない: {e}") from e
    return entries


def split_labeled(entries: list[dict]) -> tuple[list[dict], int]:
    """ラベル済みエントリと、未ラベル（expected_action が空）の件数を返す。"""
    labeled = [e for e in entries if e.get("expected_action") in VALID_ACTIONS]
    return labeled, len(entries) - len(labeled)


def import_candidates(
    *,
    messages: list[dict],
    decisions: list[dict],
    existing_ids: set[str],
    imported_at: str,
) -> list[dict]:
    """運用データからゴールデンセット候補を作る。

    expected_action は空（人間が後で埋める）。システムの判定は
    system_action として残し、同意ならそのまま写せるようにする。
    既にゴールデンセットにあるメールは取り込まない（追記のみ・書き換えない原則）。
    """
    decisions_by_id = {d.get("message_id"): d for d in decisions}
    candidates = []
    for message in messages:
        message_id = message["message_id"]
        if message_id in existing_ids:
            continue
        decision = decisions_by_id.get(message_id, {})
        candidates.append(
            {
                "message": message,
                "expected_action": None,
                "system_action": decision.get("action"),
                "system_reason": decision.get("reason"),
                "note": "",
                "imported_at": imported_at,
            }
        )
    return candidates


def build_inbox_from_golden(labeled: list[dict]) -> dict:
    """ラベル済みエントリから classify に渡す inbox 形式を組み立てる。"""
    return {"messages": [e["message"] for e in labeled]}


@dataclass
class ScoreReport:
    total: int
    correct: int
    accuracy: float | None
    archive_precision: float | None
    reply_recall: float | None
    confusion: dict[str, dict[str, int]]
    mismatches: list[dict] = field(default_factory=list)
    predictions: dict[str, str] = field(default_factory=dict)


def score_predictions(labeled: list[dict], decisions: list[dict]) -> ScoreReport:
    """予測と正解ラベルを突き合わせて採点する。

    - archive_precision: archive と予測したうち正解だった割合。
      false archive（重要メールの見逃し）を許さないための主要メトリクス。
    - reply_recall: 正解が reply のうち reply と予測できた割合。
    """
    predicted_by_id = {str(d.get("message_id")): d for d in decisions}

    actions = sorted(VALID_ACTIONS)
    confusion: dict[str, dict[str, int]] = {
        expected: dict.fromkeys([*actions, MISSING], 0) for expected in actions
    }
    mismatches: list[dict] = []
    predictions: dict[str, str] = {}
    correct = 0

    for entry in labeled:
        message = entry["message"]
        message_id = str(message["message_id"])
        expected = entry["expected_action"]
        decision = predicted_by_id.get(message_id)
        got = decision.get("action") if decision else MISSING
        if got not in VALID_ACTIONS:
            got = MISSING

        confusion[expected][got] += 1
        predictions[message_id] = got
        if got == expected:
            correct += 1
        else:
            mismatches.append(
                {
                    "message_id": message_id,
                    "subject": message.get("subject", ""),
                    "expected": expected,
                    "got": got,
                    "model_reason": (decision or {}).get("reason", ""),
                }
            )

    total = len(labeled)
    predicted_archive = sum(confusion[e]["archive"] for e in actions)
    expected_reply = sum(confusion["reply"].values())

    return ScoreReport(
        total=total,
        correct=correct,
        accuracy=_ratio(correct, total),
        archive_precision=_ratio(confusion["archive"]["archive"], predicted_archive),
        reply_recall=_ratio(confusion["reply"]["reply"], expected_reply),
        confusion=confusion,
        mismatches=mismatches,
        predictions=predictions,
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    """0 除算は None（未定義）として扱い、0.0 と区別する。"""
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def config_hash(paths: list[Path]) -> str:
    """プロンプト・ポリシー等のハッシュ。どの構成でこのスコアが出たかを結果に固定する。"""
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode())
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def golden_hash(labeled: list[dict]) -> str:
    """ゴールデンセットの同一性を示すハッシュ。セットが違う実験同士の比較を検出する。"""
    digest = hashlib.sha256()
    for entry in sorted(labeled, key=lambda e: str(e["message"]["message_id"])):
        digest.update(str(entry["message"]["message_id"]).encode())
        digest.update(str(entry["expected_action"]).encode())
    return digest.hexdigest()[:12]


def build_result(
    *,
    name: str,
    model: str | None,
    policy_hash: str,
    golden: str,
    report: ScoreReport,
    cost_usd: float | None,
    usage: dict,
    executed_at: str,
) -> dict:
    return {
        "name": name,
        "executed_at": executed_at,
        "model": model,
        "policy_hash": policy_hash,
        "golden_hash": golden,
        "golden_count": report.total,
        "metrics": {
            "archive_precision": report.archive_precision,
            "reply_recall": report.reply_recall,
            "accuracy": report.accuracy,
        },
        "confusion": report.confusion,
        "cost_usd": cost_usd,
        "usage": usage,
        "mismatches": report.mismatches,
        "predictions": report.predictions,
    }


def diff_results(a: dict, b: dict, golden_by_id: dict[str, dict] | None = None) -> dict:
    """2 実験の比較。メトリクス差分と「判定が変わった件」を返す。

    集計スコアは要約にすぎない。判断材料は changed の中身
    （増えた誤りが致命側 = false archive かどうか）。
    """
    warnings = []
    if a.get("golden_hash") != b.get("golden_hash"):
        warnings.append(
            "ゴールデンセットが異なる実験同士の比較。メトリクス差分は参考値にしかならない。"
        )

    metric_names = ("archive_precision", "reply_recall", "accuracy")
    metrics = {
        name: {
            "a": a["metrics"].get(name),
            "b": b["metrics"].get(name),
            "delta": _delta(a["metrics"].get(name), b["metrics"].get(name)),
        }
        for name in metric_names
    }

    preds_a = a.get("predictions", {})
    preds_b = b.get("predictions", {})
    changed = []
    for message_id in sorted(set(preds_a) | set(preds_b)):
        pa = preds_a.get(message_id, MISSING)
        pb = preds_b.get(message_id, MISSING)
        if pa == pb:
            continue
        entry = (golden_by_id or {}).get(message_id, {})
        changed.append(
            {
                "message_id": message_id,
                "subject": entry.get("message", {}).get("subject", ""),
                "expected": entry.get("expected_action"),
                "a": pa,
                "b": pb,
            }
        )

    return {
        "a_name": a.get("name"),
        "b_name": b.get("name"),
        "metrics": metrics,
        "cost": {"a": a.get("cost_usd"), "b": b.get("cost_usd")},
        "changed": changed,
        "warnings": warnings,
    }


def _delta(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return round(b - a, 4)


def format_diff(diff: dict) -> str:
    """diff 結果を人間が読むテキストに整形する。"""
    lines = [f"=== {diff['a_name']} → {diff['b_name']} ==="]
    lines.extend(f"⚠ {w}" for w in diff["warnings"])

    for name, values in diff["metrics"].items():
        a, b, delta = values["a"], values["b"], values["delta"]
        mark = ""
        if delta is not None:
            arrow = "▲" if delta > 0 else "▼" if delta < 0 else "＝"
            mark = f"  {arrow}{abs(delta)}"
            # archive_precision の低下は実害側なので目立たせる
            if name == "archive_precision" and delta < 0:
                mark += "  ← 危険な劣化"
        lines.append(f"{name}: {a} → {b}{mark}")

    cost_a, cost_b = diff["cost"]["a"], diff["cost"]["b"]
    if cost_a is not None and cost_b is not None:
        lines.append(f"cost: ${cost_a} → ${cost_b}")

    if diff["changed"]:
        lines.append(f"\n判定が変わった {len(diff['changed'])} 件:")
        for c in diff["changed"]:
            expected = c["expected"] or "?"
            lines.append(
                f"  {c['message_id']}  expected={expected}  a={c['a']}  b={c['b']}  {c['subject']}"
            )
    else:
        lines.append("\n判定が変わったメールはない。")
    return "\n".join(lines)
