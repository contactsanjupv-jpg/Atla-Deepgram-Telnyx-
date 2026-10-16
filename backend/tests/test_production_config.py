"""A production deploy must refuse to start with weak, missing or non-https configuration. Values are never reported."""
from app.config import Settings

GOOD = dict(
    ENV="production", APP_SECRET_KEY="x" * 40, DATABASE_URL="postgresql+psycopg://u:p@h/db",
    FRONTEND_URL="https://app.example.com", BACKEND_PUBLIC_URL="https://api.example.com",
    RETELL_API_KEY="k", PADDLE_API_KEY="k", PADDLE_WEBHOOK_SECRET="s", PADDLE_STARTER_PRICE_ID="pri_1",
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
