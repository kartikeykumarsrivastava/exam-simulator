import json
import os
from pathlib import Path
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import rag, store
from app.extraction import OCRUnavailable


def test_ocr_failure_is_reported_without_partial_index(monkeypatch):
    def unavailable(_):
        raise OCRUnavailable('PDF extraction/OCR service is unavailable or timed out. Start the OCR container and retry.')
    monkeypatch.setattr(rag,'extract_pdf',unavailable)
    embed=Mock()
    monkeypatch.setattr(rag,'embeddings',embed)
    result=TestClient(app).post('/courses',files={'files':('scan.pdf',b'%PDF-test')})
    assert result.status_code==503
    assert 'OCR service' in result.json()['detail']
    embed.assert_not_called()


@pytest.mark.skipif(not os.getenv('LIVE_OCR_TESTS'),reason='Requires OCR, Ollama and generated PDF fixtures')
def test_live_multifile_upload_ocr_and_embeddings(monkeypatch,tmp_path):
    # OCR service and embeddings are real; mock only S3 because LocalStack needs activation.
    s3=Mock()
    monkeypatch.setattr(store,'client',lambda _:s3)
    monkeypatch.setattr(rag,'DATA',tmp_path)
    fixtures=Path(os.getenv('OCR_FIXTURE_DIR','/workspace/artifacts'))
    response=TestClient(app).post('/courses',files=[
        ('files',('native.pdf',(fixtures/'ocr-native.pdf').read_bytes(),'application/pdf')),
        ('files',('scan.pdf',(fixtures/'ocr-scanned.pdf').read_bytes(),'application/pdf'))])
    assert response.status_code==200,response.text
    sources=response.json()['sources']
    assert [s['ocr_pages'] for s in sources]==[0,1]
    assert [s['name'] for s in sources]==['native.pdf','scan.pdf']
    record=json.loads(s3.put_object.call_args.kwargs['Body'])
    assert {c['extraction'] for c in record['chunks']}=={'native','ocr'}
    assert all(len(c['vector'])>100 for c in record['chunks'])
    assert all(c['page']==1 for c in record['chunks'])
