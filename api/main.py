from fastapi import FastAPI
from api.routes import health, slate, game, history, pipeline

app = FastAPI(title="Betting Copilot API", version="1.0.0")

app.include_router(health.router)
app.include_router(slate.router)
app.include_router(game.router)
app.include_router(history.router)
app.include_router(pipeline.router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=True)
