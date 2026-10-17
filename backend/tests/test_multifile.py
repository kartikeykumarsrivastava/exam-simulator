import json
from unittest.mock import Mock
from fastapi.testclient import TestClient
from app.main import app
from app import rag, store


def test_multiple_files_reach_combined_ingestion(monkeypatch):
    captured = []
    def ingest(documents):
        captured.extend((name, path.read_bytes()) for path, name in documents)
        return {'id':'a'*20, 'sources':[{'name':name,'pages':1} for _,name in documents]}
    monkeypatch.setattr(rag, 'ingest_many', ingest)
    response = TestClient(app).post('/courses', files=[
        ('files', ('chapter-one.pdf', b'%PDF-one', 'application/pdf')),
        ('files', ('chapter-two.pdf', b'%PDF-two', 'application/pdf'))])
    assert response.status_code == 200
    assert captured == [('chapter-one.pdf', b'%PDF-one'), ('chapter-two.pdf', b'%PDF-two')]
    assert len(response.json()['sources']) == 2


def test_bad_batch_rejected_before_ingestion_and_single_file_compatible(monkeypatch):
    ingest = Mock(return_value={'id':'a'*20})
    monkeypatch.setattr(rag, 'ingest_many', ingest)
    client = TestClient(app)
    assert client.post('/courses', files=[('files', ('ok.pdf', b'%PDF-ok')),
                                          ('files', ('bad.txt', b'not a PDF'))]).status_code == 422
    ingest.assert_not_called()
    assert client.post('/courses', files=[('files', (f'{i}.pdf', b'%PDF-ok')) for i in range(11)]).status_code == 422
    assert client.post('/courses', files={'file':('legacy.pdf', b'%PDF-ok')}).status_code == 200


def test_combined_index_keeps_document_and_page_provenance(monkeypatch, tmp_path):
    monkeypatch.setattr(rag, 'extract_pdf', lambda _:{'pages':[{'page':1,'method':'native','text':
        'Queues remove the earliest inserted item first. Stacks remove the most recently inserted item first.'}],
        'ocr_pages':0,'warnings':[]})
    monkeypatch.setattr(rag, 'DATA', tmp_path/'index')
    embed = Mock()
    embed.embed_documents.side_effect = lambda chunks:[[1,0] for _ in chunks]
    monkeypatch.setattr(rag, 'embeddings', lambda:embed)
    s3 = Mock()
    monkeypatch.setattr(store, 'client', lambda _:s3)
    first, second = tmp_path/'one.pdf', tmp_path/'two.pdf'
    first.write_bytes(b'one')
    second.write_bytes(b'two')
    result = rag.ingest_many([(first,'one.pdf'),(second,'two.pdf')])
    record = json.loads(s3.put_object.call_args.kwargs['Body'])
    assert result['pages'] == 2
    assert result['chunk_count'] == 2
    assert [c['source'] for c in record['chunks']] == ['one.pdf','two.pdf']
    assert [c['page'] for c in record['chunks']] == [1,1]
    assert len({c['id'] for c in record['chunks']}) == 2
    embed.embed_documents.assert_called_once()
