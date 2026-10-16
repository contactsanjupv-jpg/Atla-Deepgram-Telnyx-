"""
Central settings. Everything privileged is read from the environment —
nothing here is ever safe to hardcode or ship to the frontend.
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    ENV: str = "development"
    APP_SECRET_KEY: str  # used for session signing — required, no default
    FRONTEND_URL: str = "http://localhost:3000"

    # Database
    DATABASE_URL: str  # postgresql+psycopg://user:pass@host:5432/dbname

    # Redis (rate limiting, session store, background job queue)
    REDIS_URL: str = "redis://localhost:6379/0"

    # Retell — server-side only, NEVER exposed to frontend
    RETELL_API_KEY: str = ""
    RETELL_API_BASE_URL: str = "https://api.retellai.com"
    RETELL_WEBHOOK_MAX_SKEW_SECONDS: int = 300  # 5 minutes, per Retell's own replay window
    # Public URL Retell can reach (ngrok/hosted URL in dev). Used for the
    # per-agent webhook, so it must NOT be localhost when testing real calls.
    BACKEND_PUBLIC_URL: str = "http://localhost:8000"
    # Text model for the receptionist's Retell LLM. Empty = Retell's default.
    # This is the biggest per-minute cost lever — set deliberately.
    RETELL_LLM_MODEL: str = ""
    # After this many failed attempts a Retell event is parked as "dead" (kept, visible, re-queueable).
    RETELL_EVENT_MAX_ATTEMPTS: int = 8
    # Billing-lapse policy (PLACEHOLDER defaults — a business decision):
    # past_due keeps service for this many days from the day it became past_due.
    PAST_DUE_GRACE_DAYS: int = 3
    # After service is suspended, the number is kept this long (so a returning
    # customer keeps it) and then released. Each kept number costs $2/mo.
    NUMBER_RELEASE_DELAY_DAYS: int = 7
    # Automatic number purchase after a confirmed (status "active") Paddle subscription. A rejected purchase
    # (e.g. the provider account has no payment method / balance) is retried at most this often and this many times.
    AUTO_PROVISION_NUMBERS: bool = True
    AUTO_PROVISION_COUNTRY: str = "US"
    AUTO_PROVISION_RETRY_MINUTES: int = 30
    AUTO_PROVISION_MAX_ATTEMPTS: int = 10
    # Outgoing email (password reset). Any SMTP provider works. Without SMTP_HOST,
    # development logs the message instead; production logs an error (no email is sent).
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "Atla <no-reply@localhost>"
    SMTP_STARTTLS: bool = True
    PASSWORD_RESET_TTL_SECONDS: int = 3600
    # Hard ceiling on any single call (Retell allows 60s–7200s).
    AGENT_MAX_CALL_SECONDS: int = 900
    # Cap on business-knowledge text placed in the agent prompt (cost + latency).
    AGENT_KNOWLEDGE_MAX_CHARS: int = 24000
    # Free browser test calls a prospect gets BEFORE paying — lifetime, per organization AND per
    # user account (a database uniqueness rule, see services/free_test.py). Each one costs real
    # Retell money. Product policy: exactly one.
    FREE_TEST_CALLS_PER_ORG: int = 1
    # The browser auto-ends a test call after this many seconds. A paid test call must be covered
    # by at least this many available credit seconds before it starts.
    TEST_CALL_MAX_SECONDS: int = 180

    # ---- Credits (see services/credits.py). Unit: seconds in the database, minutes on screen. ----
    # Monthly included allowance per plan, in minutes. DERIVED, not guessed — python -m
    # app.workers.pricing_report recomputes them from the inputs below. Re-run it when any input
    # changes (Retell rates, your LLM choice, Paddle fees, the Paddle price) and update these.
    STARTER_INCLUDED_MINUTES: int = 180
    GROWTH_INCLUDED_MINUTES: int = 480
    # Inputs to that calculation. Provider rate: Retell reports call_cost.combined_cost in CENTS
    # (documented; consistent with the one real test call: 12.13 cents for 44 s ~ 16.5 cents/min).
    # 18 is that observed rate plus headroom. INTERNAL — never sent to customers.
    COST_BASIS_CENTS_PER_MINUTE: int = 18
    PADDLE_FEE_PERCENT: int = 5
    PADDLE_FEE_FIXED_CENTS: int = 50
    PHONE_NUMBER_CENTS_PER_MONTH: int = 200
    TARGET_GROSS_MARGIN_PERCENT: int = 50
    # Top-ups: customer-chosen amount in USD cents, priced per minute of call time.
    TOPUP_MIN_CENTS: int = 2000
    TOPUP_MAX_CENTS: int = 50000
    TOPUP_CENTS_PER_MINUTE: int = 45
    # Paddle tax category for the one-time top-up product (must be enabled on your Paddle account),
    # OR set PADDLE_TOPUP_PRODUCT_ID to reuse an existing catalog product instead.
    PADDLE_TOPUP_TAX_CATEGORY: str = "saas"
    PADDLE_TOPUP_PRODUCT_ID: str = ""
    # Calls cannot be refused mid-flight, so production calls may run this far below zero before the
    # receptionist is switched off until the customer buys credits (same sweeper as billing lapse).
    OVERAGE_GRACE_MINUTES: int = 30
    # Below this share of the monthly allowance (or fewer than this many minutes) the dashboard warns.
    LOW_CREDIT_PERCENT: int = 20

    # Paddle (billing) — server-side only
    PADDLE_ENV: str = "sandbox"  # "sandbox" | "live"
    PADDLE_API_KEY: str = ""
    PADDLE_WEBHOOK_SECRET: str = ""
    PADDLE_STARTER_PRICE_ID: str = ""  # pri_...
    PADDLE_GROWTH_PRICE_ID: str = ""  # pri_...
    PADDLE_WEBHOOK_MAX_SKEW_SECONDS: int = 300

    # Website-import structured extraction (server-side only — not the same
    # thing as "Claude powers the receptionist"; this is a one-off text
    # structuring call during onboarding, unrelated to Retell/call-time).
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_EXTRACTION_MODEL: str = "claude-sonnet-4-5"
    GROQ_API_KEY: str = ""
    GROQ_EXTRACTION_MODEL: str = "openai/gpt-oss-120b"

    # Google Calendar OAuth
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    GOOGLE_OAUTH_REDIRECT_URI: str = "http://localhost:8000/api/v1/integrations/google/callback"

    # Token encryption (Fernet key) for integration access/refresh tokens at rest
    TOKEN_ENCRYPTION_KEY: str = ""

    # Session cookie
    SESSION_COOKIE_NAME: str = "atla_session"
    SESSION_TTL_SECONDS: int = 60 * 60 * 24 * 14  # 14 days

    # Rate limiting defaults (overridable per-org later via DB, these are floor defaults)
    LOGIN_ATTEMPTS_PER_15MIN: int = 8
    SIGNUP_ATTEMPTS_PER_HOUR_PER_IP: int = 5
    WEBSITE_IMPORTS_PER_DAY_PER_ORG: int = 20
    TEST_CALLS_PER_DAY_PER_ORG: int = 30

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
