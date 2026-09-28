"""policy.toml の読み込み。

policy.toml は分類の唯一のチューニング面であり、カテゴリ定義（Jev の Choice の
criteria になる）と閾値の両方を持つ。ここは純粋なパースと検証だけを行い、
ネットワークには触らない。

action（archive / star）はカテゴリ定義側に書かれており、モデルには渡さない。
モデルはカテゴリを選ぶだけで、受信トレイに対する操作はコードが決める。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

VALID_ACTIONS = frozenset({"archive", "star"})


@dataclass(frozen=True)
class Category:
    name: str
    action: str
    description: str


@dataclass(frozen=True)
class Thresholds:
    archive_confidence: float
    injection: float


@dataclass(frozen=True)
class Policy:
    categories: tuple[Category, ...]
    thresholds: Thresholds

    @property
    def criteria(self) -> dict[str, str]:
        """Jev の Choice に渡す criteria（カテゴリ名 → 説明文）。"""
        return {c.name: c.description for c in self.categories}

    def action_for(self, category_name: str) -> str | None:
        """カテゴリ名に対応する action。未知のカテゴリは None。"""
        for category in self.categories:
            if category.name == category_name:
                return category.action
        return None


class PolicyError(ValueError):
    pass


def load_policy(path: Path) -> Policy:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise PolicyError(f"{path} が TOML としてパースできない: {e}") from e

    categories = _parse_categories(raw.get("category", []), path)
    return Policy(categories=categories, thresholds=_parse_thresholds(raw.get("thresholds", {})))


def _parse_categories(entries: list, path: Path) -> tuple[Category, ...]:
    if not entries:
        raise PolicyError(f"{path} に [[category]] が 1 件もない")

    categories = []
    seen: set[str] = set()
    for entry in entries:
        name = entry.get("name", "")
        action = entry.get("action")
        description = entry.get("description", "")
        if not name or not description:
            raise PolicyError(f"{path}: name と description は必須（{entry!r}）")
        if action not in VALID_ACTIONS:
            raise PolicyError(f"{path}: category {name!r} の action が不正: {action!r}")
        if name in seen:
            raise PolicyError(f"{path}: category 名が重複: {name!r}")
        seen.add(name)
        categories.append(Category(name=name, action=action, description=description))

    # Choice のオプション数上限（TypeSafe API の制約）
    if len(categories) > 255:
        raise PolicyError(f"{path}: category が 255 件を超えている（{len(categories)} 件）")
    return tuple(categories)


def _parse_thresholds(raw: dict) -> Thresholds:
    return Thresholds(
        archive_confidence=_probability(raw, "archive_confidence"),
        injection=_probability(raw, "injection"),
    )


def _probability(raw: dict, key: str) -> float:
    if key not in raw:
        raise PolicyError(f"[thresholds] に {key} がない")
    value = raw[key]
    if not isinstance(value, int | float) or isinstance(value, bool) or not 0.0 <= value <= 1.0:
        raise PolicyError(f"[thresholds] {key} は 0..1 の数値である必要がある: {value!r}")
    return float(value)
