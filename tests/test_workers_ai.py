"""Cloudflare Workers AI アダプタのテスト。

ネットワークには出ない。httpx2.MockTransport でトランスポートを差し替え、
リクエストの形・レスポンスの解釈・再試行の判断だけを検証する。
"""

import asyncio
import json

import httpx2
import pytest
from typesafe_sdk import Choice, Noul, SystemOneResponse

from numa_inbox_zero import classify as classify_mod
from numa_inbox_zero.classify import (
    QUESTION_CATEGORY,
    QUESTION_INJECTION,
    ClassifyError,
    WorkersAIClient,
    serialize_questions,
    unwrap_workers_ai_payload,
)

ACCOUNT_ID = "acct-123"
API_TOKEN = "cf-token"

SYSTEM_ONE_PAYLOAD = {
    "model": "jev-1.13.0",
    "usage": {"input_tokens": 426, "output_tokens": 73},
    "answers": {
        QUESTION_CATEGORY: {
            "type": "choice",
            "choice": "delivery_notice",
            "confidence": 0.91,
            "probabilities": {"delivery_notice": 0.94, "invoice_payment": 0.06},
        },
        QUESTION_INJECTION: {"type": "noul", "noul": 0.01},
    },
}


class Recorder:
    """MockTransport のハンドラ。リクエストを記録し、決めた応答を順に返す。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return response

    @property
    def bodies(self):
        return [json.loads(r.content) for r in self.requests]


def _ok(payload=None):
    return httpx2.Response(200, json=payload if payload is not None else SYSTEM_ONE_PAYLOAD)


def _call(recorder, *, base_url=None, state="本文", questions=None):
    """アダプタを 1 回呼ぶ。"""

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(recorder)) as http:
            client = WorkersAIClient(
                account_id=ACCOUNT_ID,
                api_token=API_TOKEN,
                http_client=http,
                **({"base_url": base_url} if base_url else {}),
            )
            return await client.system_one(
                state,
                questions if questions is not None else {"q": Noul(instructions="x")},
                model="typesafe/jev",
            )

    return asyncio.run(run())


class TestSerializeQuestions:
    def test_SDKの型がJSONに落ちる(self):
        payload = serialize_questions(
            {
                QUESTION_CATEGORY: Choice(instructions="c", criteria={"a": "A", "b": None}),
                QUESTION_INJECTION: Noul(instructions="n", criteria={"true": "T", "false": "F"}),
            }
        )
        assert payload[QUESTION_CATEGORY]["type"] == "choice"
        assert payload[QUESTION_CATEGORY]["criteria"] == {"a": "A", "b": None}
        assert payload[QUESTION_INJECTION]["type"] == "noul"

    def test_criteriaのtrueとfalseキーが保たれる(self):
        """by_alias を外すと Python の予約語回避でキー名が変わり、Noul の意味が壊れる。"""
        payload = serialize_questions({"n": Noul(instructions="n", criteria={"true": "T"})})
        assert "true" in payload["n"]["criteria"]

    def test_素のdictはそのまま通す(self):
        raw = {"n": {"type": "noul", "instructions": "n"}}
        assert serialize_questions(raw) == raw

    def test_JSONとして直列化できる(self):
        payload = serialize_questions({"c": Choice(instructions="c", criteria={"a": None})})
        assert json.loads(json.dumps(payload)) == payload


class TestUnwrapWorkersAIPayload:
    def test_包まれていない形をそのまま返す(self):
        assert unwrap_workers_ai_payload(SYSTEM_ONE_PAYLOAD) == SYSTEM_ONE_PAYLOAD

    def test_resultで包まれた形を取り出す(self):
        """Cloudflare の REST は通常 result / success / errors で包む。"""
        wrapped = {"result": SYSTEM_ONE_PAYLOAD, "success": True, "errors": [], "messages": []}
        assert unwrap_workers_ai_payload(wrapped) == SYSTEM_ONE_PAYLOAD

    def test_successがfalseならClassifyError(self):
        body = {"result": None, "success": False, "errors": [{"code": 7003, "message": "bad"}]}
        with pytest.raises(ClassifyError, match="エラーを返した"):
            unwrap_workers_ai_payload(body)

    def test_解釈できない形はClassifyError(self):
        with pytest.raises(ClassifyError, match="解釈できない"):
            unwrap_workers_ai_payload({"unexpected": 1})

    def test_JSONオブジェクトでなければClassifyError(self):
        with pytest.raises(ClassifyError, match="JSON オブジェクトではない"):
            unwrap_workers_ai_payload([1, 2, 3])


class TestWorkersAIRequest:
    def test_WorkersAIの推論エンドポイントを叩く(self):
        recorder = Recorder([_ok()])
        _call(recorder)
        assert (
            str(recorder.requests[0].url)
            == f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/run"
        )

    def test_CFトークンをBearerで送る(self):
        recorder = Recorder([_ok()])
        _call(recorder)
        assert recorder.requests[0].headers["authorization"] == f"Bearer {API_TOKEN}"

    def test_bodyはmodelとinputの形(self):
        """TypeSafe 直叩きは state / questions が top-level だが、Workers AI は input に包む。"""
        recorder = Recorder([_ok()])
        _call(recorder, state={"subject": "件名"})
        body = recorder.bodies[0]
        assert body["model"] == "typesafe/jev"
        assert body["input"]["state"] == {"subject": "件名"}
        assert "q" in body["input"]["questions"]
        assert "state" not in body

    def test_base_urlを差し替えられる(self):
        """同じ API 形を話すプロキシを挟めるようにしておく。"""
        recorder = Recorder([_ok()])
        _call(recorder, base_url="https://gateway.example.com/v1/acct/gw/workers-ai")
        assert str(recorder.requests[0].url).startswith("https://gateway.example.com/v1/acct/gw/")


class TestWorkersAIResponse:
    def test_包まれていないレスポンスを解釈できる(self):
        response = _call(Recorder([_ok()]))
        assert isinstance(response, SystemOneResponse)
        assert response.choices[QUESTION_CATEGORY].choice == "delivery_notice"
        assert response.nouls[QUESTION_INJECTION].noul == 0.01

    def test_resultで包まれたレスポンスも解釈できる(self):
        wrapped = {"result": SYSTEM_ONE_PAYLOAD, "success": True, "errors": []}
        response = _call(Recorder([_ok(wrapped)]))
        assert response.choices[QUESTION_CATEGORY].confidence == 0.91

    def test_モデルバージョンと使用量が取れる(self):
        """バージョン固定ができない代わりに、応答したバージョンを記録して drift を検知する。"""
        response = _call(Recorder([_ok()]))
        assert response.model == "jev-1.13.0"
        assert response.usage.input_tokens == 426


class TestWorkersAIRetry:
    @pytest.fixture(autouse=True)
    def _no_backoff(self, monkeypatch):
        monkeypatch.setattr(classify_mod, "BACKOFF_BASE_SECONDS", 0.0)

    def test_429は再試行して成功する(self):
        recorder = Recorder([httpx2.Response(429, text="rate limited"), _ok()])
        response = _call(recorder)
        assert len(recorder.requests) == 2
        assert response.model == "jev-1.13.0"

    def test_5xxも再試行する(self):
        recorder = Recorder([httpx2.Response(503, text="unavailable"), _ok()])
        _call(recorder)
        assert len(recorder.requests) == 2

    def test_401は再試行せず即エラー(self):
        """トークンが無効なときに無駄な再試行で待たされない。"""
        recorder = Recorder([httpx2.Response(401, text="unauthorized")])
        with pytest.raises(ClassifyError, match="HTTP 401"):
            _call(recorder)
        assert len(recorder.requests) == 1

    def test_422も再試行しない(self):
        recorder = Recorder([httpx2.Response(422, text="bad questions")])
        with pytest.raises(ClassifyError, match="HTTP 422"):
            _call(recorder)
        assert len(recorder.requests) == 1

    def test_429が続けば上限で諦める(self):
        recorder = Recorder([httpx2.Response(429, text="rate limited")])
        with pytest.raises(ClassifyError, match="HTTP 429"):
            _call(recorder)
        assert len(recorder.requests) == classify_mod.MAX_ATTEMPTS


class TestRetryDelay:
    def test_RetryAfterヘッダに従う(self):
        response = httpx2.Response(429, headers={"retry-after": "2.5"})
        assert classify_mod._retry_delay(response, attempt=1) == 2.5

    def test_ヘッダが無ければ指数バックオフ(self):
        response = httpx2.Response(429)
        delays = [classify_mod._retry_delay(response, attempt=i) for i in (1, 2, 3)]
        assert delays == [
            classify_mod.BACKOFF_BASE_SECONDS,
            classify_mod.BACKOFF_BASE_SECONDS * 2,
            classify_mod.BACKOFF_BASE_SECONDS * 4,
        ]

    def test_不正なヘッダは無視して指数バックオフ(self):
        response = httpx2.Response(429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})
        assert classify_mod._retry_delay(response, attempt=1) == classify_mod.BACKOFF_BASE_SECONDS


class TestRunJevClassifierWiring:
    """CLI の入口から Workers AI までの配線を 1 本通して確認する。"""

    def test_policyからquestionsを組みWorkersAIを叩いて判定になる(self, monkeypatch):
        from numa_inbox_zero.policy import Category, Policy, Thresholds

        policy = Policy(
            categories=(
                Category("delivery_notice", "archive", "Shipping notification"),
                Category("invoice_payment", "star", "Invoice"),
            ),
            thresholds=Thresholds(archive_confidence=0.85, injection=0.5),
        )
        recorder = Recorder([_ok()])
        # パッチ前の本体を捕まえてから差し替える（lambda が自分を呼び戻さないように）
        real_client = httpx2.AsyncClient
        monkeypatch.setattr(
            classify_mod.httpx2,
            "AsyncClient",
            lambda: real_client(transport=httpx2.MockTransport(recorder)),
        )

        outcome = classify_mod.run_jev_classifier(
            messages=[
                {
                    "message_id": "m1",
                    "from": "Shop <info@shop.example.com>",
                    "subject": "お届け予定",
                    "body": "本日お届けします",
                }
            ],
            policy=policy,
            account_id=ACCOUNT_ID,
            api_token=API_TOKEN,
        )

        # policy のカテゴリが criteria として送られている
        sent = recorder.bodies[0]["input"]["questions"][QUESTION_CATEGORY]["criteria"]
        assert set(sent) == {"delivery_notice", "invoice_payment"}
        # state に message_id が混ざっていない（モデルに操作対象を選ばせない）
        assert "m1" not in json.dumps(recorder.bodies[0]["input"]["state"], ensure_ascii=False)
        # 判定とコストが組み立てられている
        assert outcome.decisions[0]["message_id"] == "m1"
        assert outcome.decisions[0]["action"] == "archive"
        assert outcome.backend == "cloudflare-workers-ai"
        assert outcome.model == "jev-1.13.0"
        assert outcome.usage["input_tokens"] == 426
        assert outcome.total_cost_usd == round(426 * 0.042 / 1_000_000, 8)
