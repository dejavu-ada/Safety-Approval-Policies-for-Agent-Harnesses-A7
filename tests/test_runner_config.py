from argparse import Namespace

from scripts.run_experiment import _new_api_client, _pricing, _resolve_model_id, parse_args


def test_openrouter_key_creates_client_with_openrouter_endpoint(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.example/api/v1")
    client = _new_api_client()
    assert client.api_key == "sk-or-test-key"
    assert str(client.base_url).rstrip("/") == "https://openrouter.example/api/v1"
    client.close()


def test_runner_defaults_to_openrouter_slug_and_maps_short_gpt_alias(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr("sys.argv", ["run_experiment.py"])
    args = parse_args()
    assert args.agent_model == "openai/gpt-4o-mini"
    assert _resolve_model_id("gpt-4o") == "openai/gpt-4o"
    assert _resolve_model_id("anthropic/some-model") == "anthropic/some-model"


def test_unknown_provider_model_keeps_tokens_without_guessing_price():
    args = Namespace(
        agent_model="anthropic/some-model",
        reviewer_model="anthropic/some-model",
        agent_input_rate=None,
        agent_output_rate=None,
        agent_cached_input_rate=None,
        reviewer_input_rate=None,
        reviewer_output_rate=None,
        reviewer_cached_input_rate=None,
    )
    pricing = _pricing(args)
    assert pricing["agent"].input_usd_per_million is None
    assert pricing["reviewer"].output_usd_per_million is None
