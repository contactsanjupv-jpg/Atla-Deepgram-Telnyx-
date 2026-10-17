"""A production deploy must refuse to start with weak, missing or non-https configuration. Values are never reported."""
from app.config import Settings

GOOD = dict(
    ENV="production", APP_SECRET_KEY="x" * 40, DATABASE_URL="postgresql+psycopg://u:p@h/db",
    FRONTEND_URL="https://app.example.com", BACKEND_PUBLIC_URL="https://api.example.com",
    PADDLE_ENV="live", RETELL_API_KEY="k", PADDLE_API_KEY="k", PADDLE_WEBHOOK_SECRET="s", PADDLE_STARTER_PRICE_ID="pri_1",
    PADDLE_GROWTH_PRICE_ID="pri_2", TOKEN_ENCRYPTION_KEY="t", SMTP_HOST="smtp.example.com",
)


def _settings(**over):
    return Settings(_env_file=None, **{**GOOD, **over})


def test_a_complete_production_configuration_has_no_problems():
    assert _settings().production_config_problems() == []


def test_each_unsafe_setting_is_reported_by_name_only():
    cases = {
        "APP_SECRET_KEY": dict(APP_SECRET_KEY="short"),
        "FRONTEND_URL": dict(FRONTEND_URL="http://app.example.com"),
        "BACKEND_PUBLIC_URL": dict(BACKEND_PUBLIC_URL="http://localhost:8000"),
        "RETELL_API_KEY": dict(RETELL_API_KEY=""),
        "PADDLE_API_KEY": dict(PADDLE_API_KEY=""),
        "PADDLE_WEBHOOK_SECRET": dict(PADDLE_WEBHOOK_SECRET=""),
        "PADDLE_STARTER_PRICE_ID": dict(PADDLE_STARTER_PRICE_ID=""),
        "PADDLE_GROWTH_PRICE_ID": dict(PADDLE_GROWTH_PRICE_ID=""),
        "TOKEN_ENCRYPTION_KEY": dict(TOKEN_ENCRYPTION_KEY=""),
        "SMTP_HOST": dict(SMTP_HOST=""),
    }
    for name, over in cases.items():
        problems = _settings(**over).production_config_problems()
        assert len(problems) == 1 and problems[0].startswith(name), (name, problems)
        assert "short" not in problems[0] or name == "APP_SECRET_KEY"


def test_the_app_refuses_to_start_in_production_with_bad_config():
    import importlib
    import sys

    import pytest

    import app.main as main

    bad = _settings(RETELL_API_KEY="", FRONTEND_URL="http://x")
    saved = main.settings
    try:
        main.settings = bad
        src = open(main.__file__).read()
        guard = src[src.index("if settings.is_production:"):src.index("app = FastAPI(")]
        with pytest.raises(RuntimeError, match="Unsafe production configuration"):
            exec(compile(guard, "guard", "exec"), {"settings": bad})
    finally:
        main.settings = saved


# ---------------- Paddle environment safety ----------------

import pytest
from pydantic import ValidationError


def test_paddle_env_defaults_to_sandbox_outside_production_and_accepts_only_the_two_values():
    assert Settings(_env_file=None).PADDLE_ENV == "sandbox"
    assert Settings(_env_file=None, PADDLE_ENV="live").PADDLE_ENV == "live"
    for bad in ("prod", "production", "Live ", "", "SANDBOX"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, PADDLE_ENV=bad)


def test_production_with_live_paddle_is_accepted():
    assert _settings(PADDLE_ENV="live").production_config_problems() == []


def test_production_must_set_paddle_env_explicitly():
    good = {k: v for k, v in GOOD.items() if k != "PADDLE_ENV"}
    problems = Settings(_env_file=None, **good).production_config_problems()
    assert len(problems) == 1 and problems[0].startswith("PADDLE_ENV")


def test_sandbox_in_production_is_refused_unless_deliberately_allowed():
    problems = _settings(PADDLE_ENV="sandbox").production_config_problems()
    assert len(problems) == 1 and problems[0].startswith("PADDLE_ENV") and "ALLOW_SANDBOX_BILLING_IN_PRODUCTION" in problems[0]
    assert _settings(PADDLE_ENV="sandbox", ALLOW_SANDBOX_BILLING_IN_PRODUCTION=True).production_config_problems() == []


def test_contradictory_key_and_environment_are_refused_without_revealing_the_key():
    live_with_sandbox_key = _settings(PADDLE_ENV="live", PADDLE_API_KEY="pdl_sdbx_apikey_SECRETVALUE").production_config_problems()
    assert len(live_with_sandbox_key) == 1 and live_with_sandbox_key[0].startswith("PADDLE_API_KEY")
    sandbox_with_live_key = _settings(
        PADDLE_ENV="sandbox", ALLOW_SANDBOX_BILLING_IN_PRODUCTION=True, PADDLE_API_KEY="pdl_live_apikey_SECRETVALUE"
    ).production_config_problems()
    assert len(sandbox_with_live_key) == 1 and sandbox_with_live_key[0].startswith("PADDLE_API_KEY")
    assert "SECRETVALUE" not in " ".join(live_with_sandbox_key + sandbox_with_live_key)


def test_missing_paddle_configuration_in_production_is_reported_by_name():
    problems = _settings(PADDLE_API_KEY="", PADDLE_WEBHOOK_SECRET="").production_config_problems()
    assert {p.split(" ")[0] for p in problems} == {"PADDLE_API_KEY", "PADDLE_WEBHOOK_SECRET"}


def test_the_provider_selects_the_api_host_from_paddle_env_and_never_guesses(monkeypatch):
    from app.providers.billing import paddle_billing_provider as mod

    assert mod._BASE_URLS == {"sandbox": "https://sandbox-api.paddle.com", "live": "https://api.paddle.com"}
    for env, host in mod._BASE_URLS.items():
        monkeypatch.setattr(mod, "get_settings", lambda env=env: Settings(_env_file=None, PADDLE_ENV=env, PADDLE_API_KEY="k"))
        assert mod.PaddleBillingProvider()._base_url == host
    # a value that bypassed validation still cannot silently pick a host
    bogus = Settings(_env_file=None, PADDLE_API_KEY="k")
    object.__setattr__(bogus, "PADDLE_ENV", "prod")
    monkeypatch.setattr(mod, "get_settings", lambda: bogus)
    with pytest.raises(RuntimeError, match="PADDLE_ENV"):
        mod.PaddleBillingProvider()
