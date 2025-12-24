from fastapi import Header, HTTPException

from .settings import get_settings


def require_knova_secret(x_knova_secret: str = Header(..., alias="X-Knova-Secret")) -> None:
    settings = get_settings()
    if x_knova_secret != settings.knova_agent_secret:
        raise HTTPException(status_code=401, detail="Invalid secret")
