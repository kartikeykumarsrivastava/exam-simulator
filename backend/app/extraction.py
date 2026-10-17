import os
from pathlib import Path
import httpx


class OCRUnavailable(RuntimeError):
    pass


def extract_pdf(path: Path):
    url = os.getenv('OCR_BASE_URL', 'http://localhost:8001').rstrip('/')
    try:
        with path.open('rb') as document:
            response = httpx.post(url+'/extract', files={'file':('course.pdf',document,'application/pdf')},
                                  timeout=httpx.Timeout(300,connect=5))
        if response.status_code == 422:
            raise ValueError(response.json()['detail'])
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as exc:
        raise OCRUnavailable('PDF extraction/OCR service is unavailable or timed out. Start the OCR container and retry.') from exc
