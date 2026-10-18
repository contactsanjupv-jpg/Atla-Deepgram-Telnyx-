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
    import pytest

    bad = _settings(RETELL_API_KEY="", FRONTEND_URL="http://x")
    with pytest.raises(RuntimeError, match=r"Unsafe production configuration \(api\)"):
        bad.assert_startup_safe("api")


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


# ---------------- ENV is strict ----------------

def test_env_accepts_only_the_documented_environments():
    for ok in ("development", "test", "production"):
        assert Settings(_env_file=None, ENV=ok).ENV == ok
    for bad in ("prod", "Production", "PRODUCTION", "staging", "live", "", " production", "production "):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, ENV=bad)


def test_a_mistyped_env_fails_startup_instead_of_disabling_the_production_guard(monkeypatch):
    monkeypatch.setenv("ENV", "prod")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_the_test_and_development_configurations_still_load_without_env():
    assert Settings(_env_file=None).ENV == "development"
    assert Settings(_env_file=None, ENV="test").is_production is False


# ---------------- one guard for the API and the worker ----------------

def test_the_same_guard_runs_in_the_api_and_the_worker_and_names_only_settings():
    import pytest

    bad = _settings(APP_SECRET_KEY="short-secret-VALUE", RETELL_API_KEY="")
    messages = []
    for process in ("api", "worker"):
        with pytest.raises(RuntimeError) as e:
            bad.assert_startup_safe(process)
        messages.append(str(e.value))
    assert messages[0].replace("(api)", "(worker)") == messages[1]
    assert "APP_SECRET_KEY" in messages[0] and "RETELL_API_KEY" in messages[0] and "short-secret-VALUE" not in messages[0]


def test_non_production_never_blocks_startup_and_a_good_production_config_passes():
    Settings(_env_file=None, ENV="development", APP_SECRET_KEY="short").assert_startup_safe("worker")
    _settings().assert_startup_safe("worker")


def test_both_entry_points_call_the_guard_before_doing_anything():
    import inspect

    import app.main as main
    from app.workers import run_worker

    assert 'assert_startup_safe("api")' in inspect.getsource(main)
    src = inspect.getsource(run_worker.main)
    assert 'assert_startup_safe("worker")' in src and src.index("assert_startup_safe") < src.index("mark_started")


# ---------------- API and worker must resolve the same environment ----------------

def test_the_config_fingerprint_changes_with_any_environment_difference_and_ignores_secrets():
    base = _settings().config_fingerprint()
    assert base == _settings().config_fingerprint() and len(base) == 16
    for over in (dict(PADDLE_ENV="sandbox", ALLOW_SANDBOX_BILLING_IN_PRODUCTION=True), dict(FRONTEND_URL="https://other.example.com"),
                 dict(BACKEND_PUBLIC_URL="https://api2.example.com"), dict(DATABASE_URL="postgresql+psycopg://u:p@h/other"),
                 dict(PADDLE_STARTER_PRICE_ID="pri_other"), dict(PAST_DUE_GRACE_DAYS=9)):
        assert _settings(**over).config_fingerprint() != base, over
    # credentials and passwords are not part of it
    assert _settings(PADDLE_API_KEY="another", DATABASE_URL="postgresql+psycopg://u:different@h/db").config_fingerprint() == base


def test_a_config_mismatch_between_api_and_worker_raises_a_critical_alert():
    from app.services import monitoring

    r = monitoring._redis
    r.delete("atla:config:api", "atla:config:worker")
    assert monitoring.config_mismatch() is False
    r.set("atla:config:api", "aaaa")
    assert monitoring.config_mismatch() is False  # one side unknown is not a mismatch
    r.set("atla:config:worker", "aaaa")
    assert monitoring.config_mismatch() is False
    r.set("atla:config:worker", "bbbb")
    assert monitoring.config_mismatch() is True
    r.delete("atla:config:api", "atla:config:worker")


def test_publishing_stores_this_processs_fingerprint():
    from app.config import get_settings
    from app.services import monitoring

    monitoring.publish_config_fingerprint("api")
    monitoring.publish_config_fingerprint("worker")
    assert monitoring._redis.get("atla:config:api") == get_settings().config_fingerprint() == monitoring._redis.get("atla:config:worker")
    assert monitoring.config_mismatch() is False
    monitoring._redis.delete("atla:config:api", "atla:config:worker")


# ---------------- backend-authoritative Paddle environment for the browser ----------------

def test_the_public_config_endpoint_reports_the_backend_environment_and_nothing_secret(monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    c = TestClient(app)
    for env in ("sandbox", "live"):
        monkeypatch.setattr(get_settings(), "PADDLE_ENV", env)
        monkeypatch.setattr(get_settings(), "PADDLE_API_KEY", "pdl_live_apikey_SECRET")
        r = c.get("/api/v1/public-config")
        assert r.status_code == 200 and r.json() == {"paddle_env": env}
        assert "SECRET" not in r.text
