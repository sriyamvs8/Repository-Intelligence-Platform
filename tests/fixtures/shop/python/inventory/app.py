from fastapi import FastAPI

from .api.routes import router

app = FastAPI(title="inventory")
app.include_router(router, prefix="/inventory")
