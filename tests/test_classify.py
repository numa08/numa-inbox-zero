"""classify.py（Jev への入力組み立てと判定の解釈）のテスト。

ネットワークには出ない。API クライアントはスタブに差し替え、レスポンスは
SDK の実型（SystemOneResponse）に通してから解釈させる。
"""

import asyncio

import pytest
from typesafe_sdk import SystemOneResponse

from numa_inbox_zero.classify import (
    QUESTION_CATEGORY,
    QUESTION_INJECTION,
    ClassifyError,
    build_decision,
    build_questions,
    build_state,
    classify_messages,
    questions_payload,
    run_jev_classifier,
)
from numa_inbox_zero.policy import Category, Policy, Thresholds

POLICY = Policy(
    categories=(
        Category("newsletter_promotion", "archive", "Newsletter. メールマガジン"),
        Category("invoice_payment", "star", "Invoice. 請求書"),
        Category("other", "star", "None of the above. どれにも当てはまらない"),
    ),
    thresholds=Thresholds(archive_confidence=0.85, injection=0.5),
)


def _message(message_id="m1", **kw):
    base = {
        "message_id": message_id,
        "thread_id": f"t-{message_id}",
        "from": "Shop Support <support@shop.example.com>",
        "to": "me@example.com",
        "subject": "発送のお知らせ",
        "date": "Mon, 28 Sep 2026 09:00:00 +0900",
        "body": "ご注文の品を発送しました。",
    }
    return {**base, **kw}


def _response(category="newsletter_promotion", confidence=0.95, injection=0.02, tokens=1000):
    """Jev の 1 レスポンスを SDK の型で作る。"""
    others = {c.name: 0.0 for c in POLICY.categories if c.name != category}
    return SystemOneResponse.model_validate(
        {
            "model": "jev-1.13.0",
            "usage": {"input_tokens": tokens, "output_tokens": 20},
            "answers": {
                QUESTION_CATEGORY: {
                    "type": "choice",
                    "choice": category,
                    "confidence": confidence,
                    "probabilities": {category: 1.0, **others},
                },
                QUESTION_INJECTION: {"type": "noul", "noul": injection},
            },
        }
    )


class StubClient:
    """system_one だけを持つスタブ。呼び出し内容を記録する。"""

    def __init__(self, response_for=None, fail_ids=()):
        self.calls = []
        self.response_for = response_for or (lambda state: _response())
        self.fail_ids = set(fail_ids)

    async def system_one(self, state, questions, *, model=None, timeout=None):
        self.calls.append({"state": state, "questions": questions, "model": model})
        if state["subject"] in self.fail_ids:
            raise RuntimeError("429 Too Many Requests")
        return self.response_for(state)


class TestBuildState:
    def test_判定に使う項目だけを送る(self):
        state = build_state(_message())
        assert set(state) == {"sender_name", "sender_domain", "subject", "body"}
        assert state["subject"] == "発送のお知らせ"
        assert state["body"] == "ご注文の品を発送しました。"

    def test_送信者はドメインと表示名までしか送らない(self):
        """メールアドレスのローカル部を外部 API に出さない。"""
        state = build_state(_message())
        assert state["sender_domain"] == "shop.example.com"
        assert state["sender_name"] == "Shop Support"
        assert "support@shop.example.com" not in str(state)

    def test_宛先や日付は送らない(self):
        """自分のアドレスは送らない。日付は判定に効かず state を膨らませるだけ。"""
        state = build_state(_message())
        assert "me@example.com" not in str(state)
        assert "2026" not in str(state)

    def test_message_idを送らない(self):
        """モデルが操作対象のメールを指名できないようにする。"""
        state = build_state(_message())
        assert "m1" not in str(state)


class TestBuildQuestions:
    def test_categoryのcriteriaはpolicyのカテゴリ(self):
        questions = build_questions(POLICY)
        criteria = questions[QUESTION_CATEGORY].criteria
        assert set(criteria) == {"newsletter_promotion", "invoice_payment", "other"}

    def test_actionはモデルに渡さない(self):
        """archive / star の対応はコード側の責務。モデルはカテゴリだけを選ぶ。"""
        payload = questions_payload(POLICY)
        assert "archive" not in str(payload["category"]["criteria"])

    def test_injectionのNoulが同梱される(self):
        questions = build_questions(POLICY)
        assert questions[QUESTION_INJECTION].type == "noul"

    def test_questions_payloadは決定的(self):
        assert questions_payload(POLICY) == questions_payload(POLICY)


class TestBuildDecision:
    def test_actionはpolicyのカテゴリ定義から引く(self):
        decision = build_decision(
            message_id="m1", response=_response("newsletter_promotion"), policy=POLICY
        )
        assert decision["action"] == "archive"
        assert decision["category"] == "newsletter_promotion"
        assert decision["confidence"] == 0.95

    def test_starカテゴリはstarになる(self):
        decision = build_decision(
            message_id="m1", response=_response("invoice_payment"), policy=POLICY
        )
        assert decision["action"] == "star"

    def test_policyに無いカテゴリはactionがNone(self):
        """policy とモデルの応答がずれたら validate 側で拒否させる。"""
        decision = build_decision(
            message_id="m1", response=_response("not_in_policy"), policy=POLICY
        )
        assert decision["action"] is None

    def test_message_idは呼び出し側の値がそのまま入る(self):
        decision = build_decision(message_id="from-code", response=_response(), policy=POLICY)
        assert decision["message_id"] == "from-code"

    def test_reasonは判定の内訳から生成される(self):
        decision = build_decision(message_id="m1", response=_response(), policy=POLICY)
        assert "newsletter_promotion" in decision["reason"]
        assert "confidence" in decision["reason"]


class TestClassifyMessages:
    def _run(self, messages, client, **kw):
        return asyncio.run(classify_messages(messages, POLICY, client=client, **kw))

    def test_1通ごとに1リクエストになる(self):
        client = StubClient()
        outcome = self._run([_message("m1"), _message("m2", subject="請求書")], client)
        assert len(client.calls) == 2
        assert len(outcome.decisions) == 2
        assert {d["message_id"] for d in outcome.decisions} == {"m1", "m2"}

    def test_入力トークンから費用を計算する(self):
        client = StubClient(response_for=lambda state: _response(tokens=1_000_000))
        outcome = self._run([_message("m1"), _message("m2", subject="b")], client)
        assert outcome.usage["input_tokens"] == 2_000_000
        # 入力 $0.042/Mtok、出力は無料
        assert outcome.total_cost_usd == pytest.approx(0.084)

    def test_1通の失敗は他の通に波及しない(self):
        client = StubClient(fail_ids={"失敗する件名"})
        outcome = self._run(
            [_message("m1"), _message("m2", subject="失敗する件名"), _message("m3", subject="c")],
            client,
        )
        assert {d["message_id"] for d in outcome.decisions} == {"m1", "m3"}
        assert outcome.errors[0]["message_id"] == "m2"

    def test_失敗した通は判定に現れない(self):
        """判定が無ければ Gmail 上で手つかずのまま残り、次回の実行で再取得される。"""
        client = StubClient(fail_ids={"発送のお知らせ"})
        outcome = self._run([_message("m1")], client)
        assert outcome.decisions == []
        assert len(outcome.errors) == 1

    def test_バックエンド名とモデルが記録される(self):
        outcome = self._run([_message()], StubClient())
        assert outcome.backend == "cloudflare-workers-ai"
        assert outcome.model == "jev-1.13.0"

    def test_同じquestionsが全通で使われる(self):
        """ポリシーは 1 度だけ組み立てて再利用する。"""
        client = StubClient()
        self._run([_message("m1"), _message("m2", subject="b")], client)
        assert client.calls[0]["questions"] is client.calls[1]["questions"]

    def test_空の入力でもAPIを呼ばない(self):
        client = StubClient()
        outcome = self._run([], client)
        assert client.calls == []
        assert outcome.decisions == []


class TestRunJevClassifier:
    def test_認証情報が無ければ即座にエラー(self):
        """トークン未設定のまま Gmail を触りに行かせない。"""
        with pytest.raises(ClassifyError, match="Cloudflare の認証情報"):
            run_jev_classifier(
                messages=[_message()], policy=POLICY, account_id="acct", api_token=None
            )

    def test_account_idが無くてもエラー(self):
        with pytest.raises(ClassifyError, match="Cloudflare の認証情報"):
            run_jev_classifier(
                messages=[_message()], policy=POLICY, account_id=None, api_token="token"
            )
