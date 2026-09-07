"""The HTTP surface, end to end against the fake upstream over ASGI."""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from model_router import fake_upstream


def chat(
    client: TestClient,
    auth: dict[str, str],
    model: str = "demo",
    text: str = "hi there",
    **extra: Any,
) -> Any:
    return client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": text}], **extra},
        headers=auth,
    )


class TestProbes:
    def test_health(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200 and response.json()["status"] == "ok"

    def test_ready_reports_database_and_breakers(self, client: TestClient) -> None:
        data = client.get("/ready").json()
        assert data["ready"] is True and data["database"] is True
        assert data["breakers"] == {"fake": "closed"}

    def test_metrics_is_prometheus_text(self, client: TestClient) -> None:
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "router_requests_total" in response.text

    def test_every_response_carries_a_request_id(self, client: TestClient) -> None:
        assert client.get("/health").headers["x-request-id"]
        assert (
            client.get("/health", headers={"x-request-id": "abc"}).headers["x-request-id"] == "abc"
        )


class TestAuth:
    def test_missing_key_is_401_in_openai_envelope(self, client: TestClient) -> None:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "demo", "messages": [{"role": "user", "content": "x"}]},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "missing_api_key"
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_unknown_key_is_401(self, client: TestClient) -> None:
        response = chat(client, {"Authorization": "Bearer mr-not-a-real-key"})
        assert response.status_code == 401 and response.json()["error"]["code"] == "invalid_api_key"

    def test_revoked_key_is_403(
        self, client: TestClient, admin: dict[str, str], tenant: dict[str, Any]
    ) -> None:
        created = client.post(
            f"/admin/tenants/{tenant['id']}/keys", headers=admin, params={"label": "temp"}
        ).json()
        headers = {"Authorization": f"Bearer {created['api_key']}"}
        assert chat(client, headers).status_code == 200
        assert client.delete(f"/admin/keys/{created['id']}", headers=admin).status_code == 204
        assert chat(client, headers).status_code == 403

    def test_admin_refuses_without_token_and_with_wrong_token(self, client: TestClient) -> None:
        assert client.post("/admin/tenants", json={"name": "x"}).status_code == 401
        assert (
            client.post(
                "/admin/tenants", json={"name": "x"}, headers={"X-Admin-Token": "wrong"}
            ).status_code
            == 401
        )

    def test_models_requires_a_key_and_lists_aliases(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        assert client.get("/v1/models").status_code == 401
        ids = [m["id"] for m in client.get("/v1/models", headers=auth).json()["data"]]
        assert "demo" in ids and "expensive" in ids


class TestCompletions:
    def test_success_is_openai_shaped_with_router_headers(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        response = chat(client, auth, text="two words")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["object"] == "chat.completion"
        assert body["model"] == "demo"
        assert body["choices"][0]["message"]["content"] == "echo: two words"
        assert body["usage"]["prompt_tokens"] == 2 and body["usage"]["completion_tokens"] == 3
        assert response.headers["x-router-provider"] == "fake"
        assert response.headers["x-router-model"] == "fake-small"
        # 2 prompt tokens at $1/M + 3 completion at $2/M
        assert response.headers["x-router-cost-usd"] == "0.00000800"

    def test_unknown_alias_is_400(self, client: TestClient, auth: dict[str, str]) -> None:
        response = chat(client, auth, model="not-a-route")
        assert response.status_code == 400 and response.json()["error"]["code"] == "model_not_found"

    def test_invalid_body_is_422(self, client: TestClient, auth: dict[str, str]) -> None:
        assert (
            client.post(
                "/v1/chat/completions", json={"model": "demo", "messages": []}, headers=auth
            ).status_code
            == 422
        )

    def test_upstream_500_becomes_502(self, client: TestClient, auth: dict[str, str]) -> None:
        response = chat(client, auth, model="broken")
        assert response.status_code == 502 and response.json()["error"]["code"] == "upstream_failed"

    def test_upstream_429_everywhere_becomes_503_with_retry_after(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        response = chat(client, auth, model="throttled")
        assert response.status_code == 503 and response.headers["Retry-After"]

    def test_upstream_400_is_not_retried_and_becomes_502(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        response = chat(client, auth, model="rejected")
        assert response.status_code == 502

    def test_fallback_to_the_next_candidate(self, client: TestClient, auth: dict[str, str]) -> None:
        fake_upstream.reset_flaky()
        response = chat(client, auth, model="recovering")
        assert response.status_code == 200
        assert response.headers["x-router-model"] == "fake-small"
        assert response.headers["x-router-attempts"] == "2"

    def test_extra_fields_pass_through(self, client: TestClient, auth: dict[str, str]) -> None:
        response = chat(client, auth, seed=42, response_format={"type": "text"})
        assert response.status_code == 200

    def test_oversized_body_is_413(self, client: TestClient, auth: dict[str, str]) -> None:
        big = "x" * 1_100_000
        response = client.post(
            "/v1/chat/completions",
            content=json.dumps({"model": "demo", "messages": [{"role": "user", "content": big}]}),
            headers={**auth, "Content-Type": "application/json"},
        )
        assert response.status_code == 413


class TestStreaming:
    def test_sse_stream_relabelled_and_terminated(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "demo",
                "messages": [{"role": "user", "content": "a b"}],
                "stream": True,
            },
            headers=auth,
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["x-router-provider"] == "fake"
            lines = [line for line in response.iter_lines() if line.startswith("data:")]
        assert lines[-1] == "data: [DONE]"
        chunks = [json.loads(line[5:]) for line in lines[:-1]]
        assert all(c["model"] == "demo" for c in chunks)
        text = "".join(c["choices"][0].get("delta", {}).get("content", "") for c in chunks)
        assert text.strip() == "echo: a b"
        assert chunks[-1]["usage"]["completion_tokens"] == 3

    def test_streamed_usage_is_recorded_against_the_tenant(
        self,
        client: TestClient,
        auth: dict[str, str],
        admin: dict[str, str],
        tenant: dict[str, Any],
    ) -> None:
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "demo",
                "messages": [{"role": "user", "content": "a b"}],
                "stream": True,
            },
            headers=auth,
        ) as response:
            list(response.iter_lines())
        usage = client.get(f"/admin/tenants/{tenant['id']}/usage", headers=admin).json()
        assert usage["requests"] == 1
        assert usage["spent_usd"] == "0.00000800"


class TestBudgetsAndRates:
    def test_budget_exhaustion_is_402_and_stops_further_spend(
        self, client: TestClient, admin: dict[str, str]
    ) -> None:
        created = client.post(
            "/admin/tenants",
            json={"name": "tight", "monthly_budget_usd": "0.00001000"},
            headers=admin,
        ).json()
        auth = {"Authorization": f"Bearer {created['api_key']}"}
        # fake-large: $10/M prompt, $20/M completion; "hi there" is 2 prompt + 3 completion
        # tokens, so one request costs $0.00008 and the second exceeds a $0.00001 budget
        assert chat(client, auth, model="expensive").status_code == 200
        response = chat(client, auth, model="expensive")
        assert response.status_code == 402
        assert response.json()["error"]["code"] == "budget_exhausted"
        usage = client.get(f"/admin/tenants/{created['id']}/usage", headers=admin).json()
        assert usage["requests"] == 1

    def test_rate_limit_is_429_with_retry_after(
        self, client: TestClient, admin: dict[str, str]
    ) -> None:
        created = client.post(
            "/admin/tenants",
            json={"name": "slow", "requests_per_minute": 60, "burst": 2},
            headers=admin,
        ).json()
        auth = {"Authorization": f"Bearer {created['api_key']}"}
        assert chat(client, auth).status_code == 200
        assert chat(client, auth).status_code == 200
        response = chat(client, auth)
        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) >= 1

    def test_usage_summary_counts_and_sums(
        self,
        client: TestClient,
        auth: dict[str, str],
        admin: dict[str, str],
        tenant: dict[str, Any],
    ) -> None:
        chat(client, auth)
        chat(client, auth)
        usage = client.get(f"/admin/tenants/{tenant['id']}/usage", headers=admin).json()
        assert usage["requests"] == 2
        assert usage["spent_usd"] == "0.00001600"
        assert usage["monthly_budget_usd"] == "1.00000000"


class TestAdmin:
    def test_duplicate_tenant_name_is_409(
        self, client: TestClient, admin: dict[str, str], tenant: dict[str, Any]
    ) -> None:
        assert (
            client.post("/admin/tenants", json={"name": tenant["name"]}, headers=admin).status_code
            == 409
        )

    def test_key_for_unknown_tenant_is_404(self, client: TestClient, admin: dict[str, str]) -> None:
        assert client.post("/admin/tenants/nope/keys", headers=admin).status_code == 404

    def test_the_raw_key_is_never_stored(
        self, client: TestClient, tenant: dict[str, Any], session_factory: Any
    ) -> None:
        from sqlalchemy import select

        from model_router import db

        with session_factory() as session:
            rows = session.execute(select(db.ApiKey)).scalars().all()
        assert rows and all(tenant["api_key"] not in (row.key_hash + row.hint) for row in rows)
        assert all(len(row.key_hash) == 64 for row in rows)
