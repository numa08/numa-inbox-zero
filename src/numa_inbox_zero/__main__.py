"""CLI エントリポイント。

サブコマンド:
  auth      OAuth フローを起動して token.json を作る（初回のみ・対話環境）
  fetch     受信トレイの未処理メールを取得して work/inbox.json に書く
  classify  work/inbox.json を Jev で分類し work/classification.json に書く
  apply     分類結果を検証して Gmail に適用する
  run       fetch → classify → apply を一括実行（ワンショット定期実行用）
  daemon    常駐してポーリング実行（fetch → classify → apply を interval ごと）
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import sys
import time
from datetime import datetime

from . import classify as classify_mod
from . import evaluate, gmail
from .apply import apply_decisions
from .config import (
    BODY_TRUNCATE_CHARS,
    FETCH_QUERY,
    MAX_MESSAGES_PER_RUN,
    Config,
    accounts_from_env,
    poll_interval_seconds,
)
from .policy import PolicyError, load_policy
from .runlog import append_jsonl, build_decision_record, load_jsonl
from .validate import gate_archive, validate_decisions


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_run_id() -> str:
    return f"{_now_iso()}_{secrets.token_hex(3)}"


def cmd_auth(cfg: Config, _args) -> int:
    gmail.get_credentials(cfg)
    print(f"認証完了（account={cfg.account}）。トークンを保存しました: {cfg.token_path}")
    return 0


def _fetch_inbox(cfg: Config) -> dict:
    """受信トレイを取得して inbox.json を書き、その内容を返す。"""
    service = gmail.get_service(cfg)
    messages = gmail.fetch_inbox_messages(
        service, FETCH_QUERY, MAX_MESSAGES_PER_RUN, BODY_TRUNCATE_CHARS
    )
    inbox = {
        "run_id": _new_run_id(),
        "fetched_at": _now_iso(),
        "query": FETCH_QUERY,
        "messages": messages,
    }
    cfg.inbox_path.write_text(json.dumps(inbox, ensure_ascii=False, indent=1), encoding="utf-8")
    return inbox


def cmd_fetch(cfg: Config, _args) -> int:
    cfg.ensure_dirs()
    inbox = _fetch_inbox(cfg)
    print(f"{len(inbox['messages'])} 件取得 → {cfg.inbox_path}")
    return 0


def cmd_classify(cfg: Config, _args) -> int:
    inbox = json.loads(cfg.inbox_path.read_text(encoding="utf-8"))
    if not inbox["messages"]:
        print("対象メールなし。分類をスキップします。")
        cfg.classification_path.write_text('{"decisions": []}\n', encoding="utf-8")
        cfg.classify_meta_path.write_text("{}\n", encoding="utf-8")
        return 0

    try:
        outcome = classify_mod.run_jev_classifier(
            messages=inbox["messages"],
            policy=load_policy(cfg.policy_path),
            account_id=cfg.cf_account_id,
            api_token=cfg.cf_api_token,
            model=cfg.model,
            base_url=cfg.cf_base_url,
            concurrency=cfg.concurrency,
        )
    except (classify_mod.ClassifyError, PolicyError) as e:
        # 設定不備（キー未設定・policy.toml の記述ミス）は traceback より 1 行のほうが読める
        print(f"分類を実行できない: {e}", file=sys.stderr)
        return 1

    cfg.classification_path.write_text(
        json.dumps({"decisions": outcome.decisions}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    meta = {
        "backend": outcome.backend,
        "model": outcome.model,
        "usage": outcome.usage,
        "total_cost_usd": outcome.total_cost_usd,
        "duration_ms": outcome.duration_ms,
        "errors": outcome.errors,
    }
    cfg.classify_meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    # 分類に失敗した通は判定が無いので Gmail 上で手つかずのまま残り、次回再取得される
    if outcome.errors:
        print(
            f"{len(outcome.errors)} 件の分類に失敗（次回の実行で再試行されます）: "
            f"{outcome.errors[0]['detail']}",
            file=sys.stderr,
        )
    print(
        f"{len(outcome.decisions)} 件分類 → {cfg.classification_path} "
        f"(cost=${outcome.total_cost_usd} {outcome.duration_ms}ms)"
    )
    # 全通失敗（キー失効・レート制限の継続等）は障害。空の判定で apply に進み、
    # 「成功した空実行」としてログに残るのを防ぐため非ゼロで返す
    if outcome.errors and not outcome.decisions:
        return 1
    return 0


def cmd_apply(cfg: Config, args) -> int:
    started_at = _now_iso()
    inbox = json.loads(cfg.inbox_path.read_text(encoding="utf-8"))
    classification = json.loads(cfg.classification_path.read_text(encoding="utf-8"))
    classify_meta = (
        json.loads(cfg.classify_meta_path.read_text(encoding="utf-8"))
        if cfg.classify_meta_path.exists()
        else {}
    )

    messages_by_id = {m["message_id"]: m for m in inbox["messages"]}
    decisions = classification.get("decisions", [])
    if len(decisions) > len(messages_by_id):
        print(
            f"decisions が入力件数を超過（{len(decisions)} > {len(messages_by_id)}）。中断します。",
            file=sys.stderr,
        )
        _log_run_error(cfg, inbox, started_at, "decisions が入力件数を超過")
        return 1

    thresholds = load_policy(cfg.policy_path).thresholds
    validation = validate_decisions(decisions, set(messages_by_id))
    # 確信度が閾値未満・インジェクションが疑われる archive は star に降格する（検証通過後に適用）
    validation.accepted, downgraded_count = gate_archive(validation.accepted, thresholds)
    if downgraded_count:
        print(f"archive {downgraded_count} 件を star に降格しました。")

    if args.dry_run:
        service = None
        processed_label_id = ""
    else:
        service = gmail.get_service(cfg)
        processed_label_id = gmail.ensure_processed_label(service)

    summary = apply_decisions(
        service,
        validation=validation,
        processed_label_id=processed_label_id,
        dry_run=args.dry_run,
    )

    # 受理された判定は降格後の内容（downgraded_from 付き）でログに残す
    decisions_by_id = {d.get("message_id"): d for d in decisions}
    decisions_by_id.update({d["message_id"]: d for d in validation.accepted})
    for record in summary.records:
        append_jsonl(
            cfg.decisions_log_path,
            build_decision_record(
                run_id=inbox["run_id"],
                account=cfg.account,
                record=record,
                decision=decisions_by_id.get(record.message_id),
                message=messages_by_id.get(record.message_id),
                log_subjects=cfg.log_subjects,
            ),
        )

    run_record = {
        "run_id": inbox["run_id"],
        "account": cfg.account,
        "started_at": started_at,
        "finished_at": _now_iso(),
        "mode": "dry-run" if args.dry_run else "apply",
        "fetch": {
            "query": inbox.get("query"),
            "candidates": len(inbox["messages"]),
            "truncated_bodies": sum(1 for m in inbox["messages"] if m.get("body_truncated")),
        },
        "classify": classify_meta,
        "apply": {
            "archived": summary.archived,
            "starred": summary.starred,
            "rejected": summary.rejected,
            "downgraded": downgraded_count,
            "errors": summary.errors,
        },
        "errors": [
            {"message_id": r.message_id, "detail": r.failure_reason}
            for r in summary.records
            if r.applied == "failed"
        ],
    }
    append_jsonl(cfg.runs_log_path, run_record)

    prefix = "[dry-run] " if args.dry_run else ""
    print(
        f"{prefix}archive={summary.archived} star={summary.starred} "
        f"rejected={summary.rejected} errors={summary.errors}"
    )
    return 0 if summary.errors == 0 else 1


def _log_run_error(cfg: Config, inbox: dict, started_at: str, message: str) -> None:
    append_jsonl(
        cfg.runs_log_path,
        {
            "run_id": inbox.get("run_id"),
            "account": cfg.account,
            "started_at": started_at,
            "finished_at": _now_iso(),
            "mode": "error",
            "errors": [{"detail": message}],
        },
    )


def cmd_eval_import(cfg: Config, _args) -> int:
    """実行ログの判定をゴールデンセット候補として取り込む。

    本文・件名の全文はログに残さない方針（SPEC §6）のため、実行ログ
    （decisions.jsonl）の message_id をもとに Gmail API から本文を再取得し
    （読み取りのみ）、ログに残る判定（action / reason）と合成する。
    """
    decisions = load_jsonl(cfg.decisions_log_path)
    targets = evaluate.select_import_targets(decisions, account=cfg.account)
    if not targets:
        print(
            f"取り込み対象がない: {cfg.decisions_log_path} に "
            f"account={cfg.account} の判定がない（--account の指定を確認）",
            file=sys.stderr,
        )
        return 1

    existing = evaluate.load_golden(cfg.golden_path)
    existing_ids = {str(e["message"]["message_id"]) for e in existing}
    new_targets = [t for t in targets if str(t["message_id"]) not in existing_ids]

    messages: list[dict] = []
    missing: list[str] = []
    if new_targets:
        service = gmail.get_service(cfg)
        messages, missing = gmail.fetch_messages_by_ids(
            service, [str(t["message_id"]) for t in new_targets], BODY_TRUNCATE_CHARS
        )

    candidates = evaluate.import_candidates(
        messages=messages,
        decisions=new_targets,
        existing_ids=existing_ids,
        imported_at=_now_iso(),
        note=f"account={cfg.account}",
    )
    cfg.eval_dir.mkdir(parents=True, exist_ok=True)
    for candidate in candidates:
        append_jsonl(cfg.golden_path, candidate)

    labeled, unlabeled = evaluate.split_labeled(existing + candidates)
    lines = [f"{len(candidates)} 件を候補として追記 → {cfg.golden_path}"]
    if missing:
        lines.append(f"取得できなかったメール {len(missing)} 件（削除済み等）: {missing}")
    lines.append(
        f"現在: ラベル済み {len(labeled)} 件 / 未ラベル {unlabeled} 件。\n"
        f"全件レビューし、誤判定だけ expected_action を上書きしてください"
        f"（null のままの行は system_action に同意として採点される）。"
    )
    print("\n".join(lines))
    return 0


def cmd_eval_run(cfg: Config, args) -> int:
    """ゴールデンセットに対して分類器を走らせ、採点結果を保存する。

    採点対象は本番と同じく降格後（gate_archive 適用後）の判定。あわせて降格前の
    生の confidence を結果に残し、閾値の掃引は `eval sweep` でオフラインに行う。
    """
    entries = evaluate.load_golden(cfg.golden_path)
    labeled, unlabeled = evaluate.split_labeled(entries)
    if not labeled:
        print(
            f"ラベル済みエントリがない: {cfg.golden_path}\n"
            "eval import で候補を取り込み、expected_action を埋めてください。",
            file=sys.stderr,
        )
        return 1
    if unlabeled:
        print(f"注意: 未ラベル {unlabeled} 件はスキップします。")

    model = args.model or cfg.model
    try:
        policy = load_policy(cfg.policy_path)
        outcome = classify_mod.run_jev_classifier(
            messages=evaluate.build_inbox_from_golden(labeled)["messages"],
            policy=policy,
            account_id=cfg.cf_account_id,
            api_token=cfg.cf_api_token,
            model=model,
            base_url=cfg.cf_base_url,
            concurrency=cfg.concurrency,
        )
    except (classify_mod.ClassifyError, PolicyError) as e:
        print(f"評価を実行できない: {e}", file=sys.stderr)
        return 1

    if outcome.errors:
        print(
            f"注意: {len(outcome.errors)} 件が分類に失敗（missing として採点されます）: "
            f"{outcome.errors[0]['detail']}",
            file=sys.stderr,
        )
    # 全件失敗なら採点する中身が無い。結果ファイルを書くと「全部 missing の実験」が
    # 正当な結果として残り、diff の比較対象になってしまうので何も書かずに落とす。
    if outcome.errors and not outcome.decisions:
        print(
            f"全 {len(outcome.errors)} 件が失敗したため結果を保存しない。"
            "認証情報・権限・プランを確認すること。",
            file=sys.stderr,
        )
        return 1

    gated, downgraded = gate_archive(outcome.decisions, policy.thresholds)
    report = evaluate.score_predictions(labeled, gated)

    result = evaluate.build_result(
        name=args.name,
        model=outcome.model or model,
        backend=outcome.backend,
        policy_hash=evaluate.config_hash([cfg.policy_path]),
        questions=evaluate.questions_hash(classify_mod.questions_payload(policy)),
        golden=evaluate.golden_hash(labeled),
        thresholds={
            "archive_confidence": policy.thresholds.archive_confidence,
            "injection": policy.thresholds.injection,
        },
        report=report,
        judgments=evaluate.build_judgments(gated),
        cost_usd=outcome.total_cost_usd,
        usage=outcome.usage,
        executed_at=_now_iso(),
    )
    cfg.eval_results_dir.mkdir(parents=True, exist_ok=True)
    result_path = cfg.eval_results_dir / f"{args.name}.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    m = result["metrics"]
    human = result["human_labeled"]
    print(
        f"=== {args.name} (model={result['model']}, n={report.total}) ===\n"
        f"archive_precision: {m['archive_precision']}\n"
        f"archive_recall:    {m['archive_recall']}\n"
        f"accuracy:          {m['accuracy']}\n"
        f"人間ラベル分:      {human['correct']}/{human['total']} 正解\n"
        f"降格:              {downgraded} 件\n"
        f"cost:              ${result['cost_usd']}\n"
        f"不一致 {len(report.mismatches)} 件 → {result_path}"
    )
    return 0


def cmd_eval_sweep(cfg: Config, args) -> int:
    """保存済みの結果に対して confidence 閾値を掃引する（API を呼ばない）。"""
    path = cfg.eval_results_dir / f"{args.name}.json"
    if not path.exists():
        print(f"結果ファイルがない: {path}", file=sys.stderr)
        return 1
    result = json.loads(path.read_text(encoding="utf-8"))
    judgments = result.get("judgments")
    if not judgments:
        print(
            f"{path} に judgments がない（Jev 以前の結果ファイルは掃引できない）。",
            file=sys.stderr,
        )
        return 1

    labeled, _ = evaluate.split_labeled(evaluate.load_golden(cfg.golden_path))
    thresholds = result.get("thresholds", {})
    injection = args.injection
    if injection is None:
        injection = thresholds.get("injection", load_policy(cfg.policy_path).thresholds.injection)

    rows = evaluate.sweep_thresholds(labeled, judgments, injection_threshold=injection)
    print(f"=== {args.name}: injection={injection} での confidence 掃引 ===")
    print(evaluate.format_sweep(rows, current=thresholds.get("archive_confidence")))
    print(
        "\nfalse_arch = 本来 star のメールをアーカイブした件数。ここを 0 に保てる最小の閾値を選ぶ。"
    )
    return 0


def cmd_eval_diff(cfg: Config, args) -> int:
    """2 つの実験結果を比較し、メトリクス差分と判定が変わった件を表示する。"""
    results = []
    for name in (args.a, args.b):
        path = cfg.eval_results_dir / f"{name}.json"
        if not path.exists():
            print(f"結果ファイルがない: {path}", file=sys.stderr)
            return 1
        results.append(json.loads(path.read_text(encoding="utf-8")))

    # null（system_action に同意）を正解として表示するため、正規化済みエントリを引く
    labeled, _ = evaluate.split_labeled(evaluate.load_golden(cfg.golden_path))
    golden_by_id = {str(e["message"]["message_id"]): e for e in labeled}
    diff = evaluate.diff_results(results[0], results[1], golden_by_id)
    print(evaluate.format_diff(diff))
    return 0


def cmd_run(cfg: Config, args) -> int:
    for step in (cmd_fetch, cmd_classify):
        code = step(cfg, args)
        if code != 0:
            return code
    return cmd_apply(cfg, args)


def _daemon_cycle(accounts: list[str], args, config_factory=Config) -> None:
    """全アカウントを 1 巡する。1 アカウントの失敗で残りを止めない。

    新着ゼロのアカウントは classify / apply に進まない。ポーリングの大半は空振りで、
    そのたびに分類 API を呼んだり runs.jsonl に空実行を記録するとコストとログが
    肥大するため。
    """
    for account in accounts:
        cfg = config_factory(account=account)
        try:
            cfg.ensure_dirs()
            inbox = _fetch_inbox(cfg)
            if not inbox["messages"]:
                continue
            print(f"[{_now_iso()}] {account}: {len(inbox['messages'])} 件検出")
            if cmd_classify(cfg, args) == 0:
                cmd_apply(cfg, args)
        except Exception as e:
            # 常駐プロセスは一時的な API 障害で死なせない。次のポーリングで再試行する。
            print(f"[{_now_iso()}] {account}: 処理に失敗: {e}", file=sys.stderr)


def _raise_keyboard_interrupt(_signum, _frame) -> None:
    raise KeyboardInterrupt


def cmd_daemon(cfg: Config, args) -> int:
    accounts = accounts_from_env() or [cfg.account]
    print(
        f"[{_now_iso()}] daemon 開始: accounts={','.join(accounts)} "
        f"interval={args.interval}s dry_run={args.dry_run}"
    )
    # タスクスケジューラ等からの終了要求（SIGTERM）でも停止ログを残して正常終了する
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    try:
        while True:
            _daemon_cycle(accounts, args)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"[{_now_iso()}] daemon 停止")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="numa-inbox-zero")
    parser.add_argument(
        "--account",
        default=os.environ.get("NIZ_ACCOUNT", "default"),
        help="対象アカウント名。トークンと作業ディレクトリがこの名前で分離される",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("auth", help="OAuth フローを起動してアカウントのトークンを作る")
    sub.add_parser("fetch", help="受信トレイの未処理メールを取得する")
    sub.add_parser("classify", help="取得済みメールを分類する")

    p_apply = sub.add_parser("apply", help="分類結果を Gmail に適用する")
    p_apply.add_argument("--dry-run", action="store_true", help="Gmail を変更せず判定のみ表示")

    p_run = sub.add_parser("run", help="fetch → classify → apply を一括実行")
    p_run.add_argument("--dry-run", action="store_true", help="Gmail への書き込みを行わない")

    p_daemon = sub.add_parser(
        "daemon", help="常駐してポーリング実行。NIZ_ACCOUNTS の全アカウントを巡回する"
    )
    p_daemon.add_argument(
        "--interval",
        type=int,
        default=poll_interval_seconds(),
        help="ポーリング間隔（秒）。既定は NIZ_POLL_INTERVAL または 300",
    )
    p_daemon.add_argument("--dry-run", action="store_true", help="Gmail への書き込みを行わない")

    p_eval = sub.add_parser("eval", help="オフライン評価（ゴールデンセットで採点）")
    eval_sub = p_eval.add_subparsers(dest="eval_command", required=True)
    eval_sub.add_parser("import", help="直近の実行分をゴールデンセット候補として取り込む")
    p_eval_run = eval_sub.add_parser("run", help="ゴールデンセットで分類器を採点する")
    p_eval_run.add_argument("--name", required=True, help="実験名。結果ファイル名になる")
    p_eval_run.add_argument("--model", default=None, help="モデル上書き（既定は NIZ_MODEL）")
    p_eval_sweep = eval_sub.add_parser(
        "sweep", help="保存済み結果に対して archive の confidence 閾値を掃引する（API 不要）"
    )
    p_eval_sweep.add_argument("name", help="掃引する実験名")
    p_eval_sweep.add_argument(
        "--injection",
        type=float,
        default=None,
        help="injection 閾値の上書き（既定は結果ファイルに記録された値）",
    )
    p_eval_diff = eval_sub.add_parser("diff", help="2 つの実験結果を比較する")
    p_eval_diff.add_argument("a", help="比較元の実験名")
    p_eval_diff.add_argument("b", help="比較先の実験名")

    args = parser.parse_args(argv)
    cfg = Config(account=args.account)

    if args.command == "eval":
        eval_handlers = {
            "import": cmd_eval_import,
            "run": cmd_eval_run,
            "sweep": cmd_eval_sweep,
            "diff": cmd_eval_diff,
        }
        return eval_handlers[args.eval_command](cfg, args)

    handlers = {
        "auth": cmd_auth,
        "fetch": cmd_fetch,
        "classify": cmd_classify,
        "apply": cmd_apply,
        "run": cmd_run,
        "daemon": cmd_daemon,
    }
    return handlers[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
