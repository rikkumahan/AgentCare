from fastapi import FastAPI

from app.routes.auth_routes import router as auth_router

app = FastAPI(title="AgentCare")

app.include_router(auth_router)
