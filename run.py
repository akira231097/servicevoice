"""Start the local console. Webhooks may be exposed via a separately configured HTTPS tunnel."""
import os
import asyncio
from pathlib import Path
from runtime_sqlite import ensure_sqlite
ensure_sqlite()
from dotenv import load_dotenv
import uvicorn

if __name__=="__main__":
    load_dotenv(Path(__file__).parent/".env")
    from app.main import app,public_app
    async def serve():
        console=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=int(os.getenv("SERVICEVOICE_PORT","8765")),proxy_headers=False))
        webhooks=uvicorn.Server(uvicorn.Config(public_app,host="127.0.0.1",port=int(os.getenv("SERVICEVOICE_WEBHOOK_PORT","8766")),proxy_headers=False))
        await asyncio.gather(console.serve(),webhooks.serve())
    asyncio.run(serve())
