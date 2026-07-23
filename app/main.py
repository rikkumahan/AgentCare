from fastapi import FastAPI

from app.routes.auth_routes import router as auth_router
from app.routes.dashboard_routes import router as dashboard_router

app = FastAPI(title="AgentCare")

app.include_router(auth_router)
app.include_router(dashboard_router)


@app.get("/health")
def health():
    return {"status": "ok"}
