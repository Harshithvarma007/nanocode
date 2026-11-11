from nanocode.config import load_dotenv, parse_model_spec, pick_local_model, DEFAULT_PROVIDERS


def test_parse_model_spec():
    assert parse_model_spec("ollama:qwen3:8b", DEFAULT_PROVIDERS) == ("ollama", "qwen3:8b")
    assert parse_model_spec("qwen3:8b", DEFAULT_PROVIDERS) == ("ollama", "qwen3:8b")
    assert parse_model_spec("claude-sonnet-5-5", DEFAULT_PROVIDERS) == ("anthropic", "claude-sonnet-5-5")
    assert parse_model_spec("anthropic", DEFAULT_PROVIDERS) == ("anthropic", "claude-opus-5-5")


def test_pick_local_model_compares_whole_family_names():
    assert pick_local_model(["qwen3.5:4b", "llama3.1:8b", "qwen3:8b"]) == "qwen3:8b"
    assert pick_local_model(["qwen3.5:4b", "llama3.1:8b"]) == "qwen3.5:4b"
    assert pick_local_model(["some-model:7b"]) == "some-model:7b"
    assert pick_local_model([]) is None


def test_load_dotenv_never_overrides(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nexport NANO_A="from-file"\nNANO_B=from-file\n')
    monkeypatch.setenv("NANO_B", "from-shell")
    monkeypatch.delenv("NANO_A", raising=False)
    load_dotenv(env)
    import os

    assert os.environ["NANO_A"] == "from-file" and os.environ["NANO_B"] == "from-shell"
