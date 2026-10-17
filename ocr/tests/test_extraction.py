import io
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter, PdfReader
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from fastapi.testclient import TestClient
from app import app, extract_document, needs_ocr

TEXT = 'Queues remove the first inserted item first. A stack removes the most recently inserted item first.'


def native_pdf():
    writer=PdfWriter()
    page=writer.add_blank_page(width=612,height=792)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    stream=DecodedStreamObject()
    stream.set_data(f'BT /F1 11 Tf 30 700 Td ({TEXT}) Tj ET'.encode())
    page[NameObject('/Contents')]=writer._add_object(stream)
    output=io.BytesIO()
    writer.write(output)
    return output.getvalue()


def scanned_pdf():
    image=Image.new('RGB',(1275,1650),'white')
    draw=ImageDraw.Draw(image)
    font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',28)
    lines=['Queues remove the first inserted item first.',
           'A stack removes the most recently inserted item first.',
           'Enqueue adds an item to the back of the queue.',
           'Dequeue removes an item from the front of the queue.']
    for i,line in enumerate(lines):draw.text((70,120+i*65),line,fill='black',font=font)
    output=io.BytesIO()
    image.save(output,format='PDF',resolution=150)
    return output.getvalue()


def test_native_text_bypasses_tesseract(monkeypatch):
    monkeypatch.setattr('app.recognize',lambda _:(_ for _ in ()).throw(AssertionError('Native page must bypass OCR')))
    result=extract_document(native_pdf())
    assert result['ocr_pages']==0
    assert result['pages'][0]['method']=='native'
    assert 'Queues remove' in result['pages'][0]['text']


def test_real_scanned_and_mixed_documents():
    writer=PdfWriter()
    for content in [native_pdf(),scanned_pdf()]:
        for page in PdfReader(io.BytesIO(content)).pages:writer.add_page(page)
    output=io.BytesIO()
    writer.write(output)
    response=TestClient(app).post('/extract',files={'file':('mixed.pdf',output.getvalue(),'application/pdf')})
    assert response.status_code==200,response.text
    result=response.json()
    assert result['ocr_pages']==1
    assert [p['method'] for p in result['pages']]==['native','ocr']
    assert [p['page'] for p in result['pages']]==[1,2]
    assert 'Queues remove the first inserted item first' in result['pages'][1]['text']
    assert result['pages'][1]['confidence']>70


def test_detection_rejects_broken_text_and_detects_image_heavy_page():
    assert needs_ocr('',0)
    assert needs_ocr('\ufffd'*100,0)
    assert needs_ocr(TEXT,.8)
    assert not needs_ocr(TEXT,.05)


def test_invalid_blank_and_encrypted_pdfs_rejected():
    client=TestClient(app)
    assert client.post('/extract',files={'file':('bad.pdf',b'not a PDF')}).status_code==422
    writer=PdfWriter()
    writer.add_blank_page(width=612,height=792)
    output=io.BytesIO()
    writer.write(output)
    assert client.post('/extract',files={'file':('blank.pdf',output.getvalue())}).status_code==422
    writer.encrypt('secret')
    output=io.BytesIO()
    writer.write(output)
    assert client.post('/extract',files={'file':('locked.pdf',output.getvalue())}).status_code==422
