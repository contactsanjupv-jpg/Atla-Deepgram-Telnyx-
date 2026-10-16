from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.v1.calls import router as calls_router
from app.api.v1.leads import router as leads_router
from app.api.v1.activation import router as activation_router
from app.api.v1.agents import router as agents_router
from app.api.v1.auth import router as auth_router
from app.api.v1.businesses import router as businesses_router
from app.api.v1.organizations import router as organizations_router
from app.api.v1.phone_numbers import router as phone_numbers_router
from app.api.v1.voices import router as voices_router
from app.config import get_settings
from app.core.error_handling import unhandled_exception_handler
from app.core.security_headers import SecurityHeadersMiddleware
from app.webhooks.retell import router as retell_webhook_router
from app.api.v1.account import router as account_router
from app.api.v1.analytics import router as analytics_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.billing import router as billing_router
from app.api.v1.admin_refunds import router as admin_refunds_router
from app.api.v1.onboarding import router as onboarding_router
from app.api.v1.plans import router as plans_router
from app.api.v1.usage import router as usage_router
from app.webhooks.paddle import router as paddle_webhook_router


settings = get_settings()

app = FastAPI(title="Atla API", version="0.1.0", docs_url=None if settings.is_production else "/docs")

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)
app.add_exception_handler(Exception, unhandled_exception_handler)

app.include_router(auth_router)
app.include_router(organizations_router)
app.include_router(businesses_router)
app.include_router(voices_router)
app.include_router(phone_numbers_router)
app.include_router(agents_router)
app.include_router(activation_router)
app.include_router(calls_router)
app.include_router(leads_router)
app.include_router(retell_webhook_router)
app.include_router(billing_router)
app.include_router(admin_refunds_router)
app.include_router(plans_router)
app.include_router(paddle_webhook_router)
app.include_router(usage_router)
app.include_router(analytics_router)
app.include_router(dashboard_router)
app.include_router(onboarding_router)
app.include_router(account_router)


@app.get("/health")
def health():
    return {"status": "ok"}
