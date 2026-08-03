"""claude -p による分類。

将来 Anthropic Messages API に差し替えられるよう、バックエンドを
関数ひとつに閉じ込める。呼び出し側は ClassifyOutcome だけを見る。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# ツール・個人設定・MCP をすべて遮断する。
# 1) メール本文中の指示が実際の操作（ファイル I/O 等）に繋がる経路を消す
# 2) input tokens が「プロンプト + 本文」だけになり API 移行時の見積もりに使える
CLAUDE_ISOLATION_FLAGS = [
    "--tools",
    "",
    "--setting-sources",
    "",
    "--strict-mcp-config",
    "--no-session-persistence",
]

CLAUDE_TIMEOUT_SECONDS = 600


@dataclass
class ClassifyOutcome:
    decisions: list[dict]
    backend: str = "claude-cli"
    model: str | None = None
    cli_version: str | None = None
    usage: dict = field(default_factory=dict)
    total_cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    raw_result: str = ""


class ClassifyError(RuntimeError):
    pass


def build_claude_command(
    *,
    model: str,
    system_prompt_path: Path,
    schema_path: Path,
) -> list[str]:
    schema = schema_path.read_text(encoding="utf-8")
    return [
        "claude",
        "-p",
        "--model",
        model,
        "--output-format",
        "json",
        "--system-prompt-file",
        str(system_prompt_path),
        "--json-schema",
        schema,
        *CLAUDE_ISOLATION_FLAGS,
    ]


def build_classifier_input(inbox: dict, policy_text: str) -> str:
    """分類器への入力を組み立てる。

    ポリシーは信頼できる指示、メールは信頼できないデータという区別を
    明示的なタグで表現する。本文はタグ内に埋め込むが、検証は apply 側で
    行うためここでのエスケープには頼らない。
    """
    messages_json = json.dumps(inbox["messages"], ensure_ascii=False, indent=1)
    return (
        f"<policy>\n{policy_text.strip()}\n</policy>\n\n<messages>\n{messages_json}\n</messages>\n"
    )


def parse_cli_output(stdout: str) -> ClassifyOutcome:
    """claude -p --output-format json の出力をパースする。

    usage 等のメタデータは CLI バージョンで構造が変わりうるため、
    欠損を許容して取れたものだけを持つ。decisions のパース失敗だけは
    致命的なので例外にする。
    """
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ClassifyError(f"claude CLI の出力が JSON ではない: {e}") from e

    if envelope.get("is_error"):
        raise ClassifyError(f"claude CLI がエラーを返した: {envelope.get('result')!r}")

    result_text = envelope.get("result", "")
    try:
        classification = json.loads(result_text)
        decisions = classification["decisions"]
        if not isinstance(decisions, list):
            raise TypeError("decisions が配列ではない")
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise ClassifyError(f"分類結果のパースに失敗: {e}") from e

    model = None
    model_usage = envelope.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        # modelUsage のキーが実際に使われたモデル ID。補助モデル（haiku 等）が
        # 混ざることがあるため、コストが最大のものを主モデルとみなす。
        model = max(model_usage, key=lambda k: model_usage[k].get("costUSD", 0))

    return ClassifyOutcome(
        decisions=decisions,
        model=model,
        usage=envelope.get("usage") or {},
        total_cost_usd=envelope.get("total_cost_usd"),
        duration_ms=envelope.get("duration_ms"),
        num_turns=envelope.get("num_turns"),
        raw_result=result_text,
    )


def run_claude_classifier(
    *,
    inbox: dict,
    policy_text: str,
    model: str,
    system_prompt_path: Path,
    schema_path: Path,
) -> ClassifyOutcome:
    command = build_claude_command(
        model=model,
        system_prompt_path=system_prompt_path,
        schema_path=schema_path,
    )
    stdin_text = build_classifier_input(inbox, policy_text)

    proc = subprocess.run(
        command,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=CLAUDE_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise ClassifyError(
            f"claude CLI が終了コード {proc.returncode} で失敗: {proc.stderr[:500]}"
        )

    outcome = parse_cli_output(proc.stdout)
    outcome.cli_version = _claude_version()
    return outcome


def _claude_version() -> str | None:
    try:
        proc = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=30)
        return proc.stdout.strip() or None
    except OSError:
        return None
