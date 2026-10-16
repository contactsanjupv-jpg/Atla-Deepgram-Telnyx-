"""
Liveness (/health, in main.py), readiness (/health/ready) and operator alerts (/health/alerts).
Responses carry booleans and counts only: no exception text, hostnames, ids or secrets.
"""
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import get_db
from app.services import monitoring

router = APIRouter(tags=["health"])


@router.get("/health/ready")
def ready(response: Response, db: Session = Depends(get_db)):
    db_ok = monitoring.check_database(db)
    redis_ok = monitoring.check_redis()
    worker = monitoring.worker_status()
    ok = db_ok and redis_ok and worker["alive"]
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if ok else "unhealthy", "database": db_ok, "redis": redis_ok, "worker": worker["alive"]}


@router.get("/health/alerts")
def alerts(authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    token = get_settings().MONITORING_TOKEN
    if not token:
        raise HTTPException(status.HTTP_404_NOT_FOUND)  # disabled until a token is configured
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied.encode(), token.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)
    found = monitoring.alert_conditions(db)
    return {"ok": not found, "alerts": [{"key": a.key, "severity": a.severity, "message": a.message, "count": a.count} for a in found]}
