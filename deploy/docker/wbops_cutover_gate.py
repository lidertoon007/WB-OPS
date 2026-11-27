"""Keep ordinary requests out until the Docker cutover passes its checks."""

from pathlib import Path

from starlette.responses import PlainTextResponse

from app.main import app as wbops

READY = Path("/run/wbops-cutover/ready")


async def app(scope, receive, send):
    if scope["type"] == "http" and not READY.is_file():
        if not (scope["path"] == "/health" and scope["method"] in {"GET", "HEAD"}):
            response = PlainTextResponse("WB Ops is switching over", status_code=503)
            await response(scope, receive, send)
            return
    await wbops(scope, receive, send)
