import json

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("")
async def get_settings():
    from narrator.app import settings
    return settings.get_all()


@router.patch("")
async def update_settings(body: dict):
    from narrator.app import settings
    from narrator.config import DEFAULTS

    updated = {}
    for key, value in body.items():
        if key not in DEFAULTS:
            continue
        settings.set(key, str(value))
        updated[key] = str(value)
    return updated


@router.get("/export")
async def export_settings():
    from narrator.app import settings
    return JSONResponse(
        content=settings.get_all(),
        headers={"Content-Disposition": "attachment; filename=narrator-settings.json"},
    )


@router.post("/import")
async def import_settings(file: UploadFile):
    from narrator.app import settings
    from narrator.config import DEFAULTS

    try:
        content = await file.read()
        data = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}") from e

    imported = 0
    for key, value in data.items():
        if key in DEFAULTS:
            settings.set(key, str(value))
            imported += 1

    return {"imported": imported}
