import logging

import httpx

log = logging.getLogger(__name__)


class ABSClient:
    def __init__(self, api_url: str, api_token: str):
        self.api_url = api_url.rstrip("/")
        self.api_token = api_token

    async def test_connection(self) -> bool:
        if not self.api_url or not self.api_token:
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self.api_url}/api/libraries",
                    headers={"Authorization": f"Bearer {self.api_token}"},
                )
                return resp.status_code == 200
        except Exception as e:
            log.warning("ABS connection test failed: %s", e)
            return False

    async def trigger_scan(self) -> bool:
        if not self.api_url or not self.api_token:
            return False
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{self.api_url}/api/libraries",
                    headers={"Authorization": f"Bearer {self.api_token}"},
                )
                resp.raise_for_status()
                libraries = resp.json().get("libraries", [])
                for lib in libraries:
                    lib_id = lib.get("id")
                    if lib_id:
                        await client.post(
                            f"{self.api_url}/api/libraries/{lib_id}/scan",
                            headers={"Authorization": f"Bearer {self.api_token}"},
                        )
                        log.info("Triggered ABS library scan: %s", lib.get("name", lib_id))
                return True
        except Exception as e:
            log.warning("ABS scan trigger failed: %s", e)
            return False
