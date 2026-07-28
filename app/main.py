from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.routes.auth_routes import router as auth_router
from app.routes.dashboard_routes import router as dashboard_router
from app.routes.request_routes import router as request_router

app = FastAPI(title="AgentCare")

app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(request_router)


@app.get("/")
def root():
    return RedirectResponse(url="/login")


@app.get("/health")
def health():
    return {"status": "ok"}
