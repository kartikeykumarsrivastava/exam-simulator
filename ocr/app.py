"""Page-aware native text extraction with selective local Tesseract OCR."""
import csv
import io
import math
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pypdfium2 as pdfium
from fastapi import FastAPI, File, UploadFile, HTTPException
from pypdf import PdfReader
from starlette.concurrency import run_in_threadpool

app = FastAPI(title='Exam Studio OCR')
# PDFium is not thread-safe. Serialize all access within this single-worker service.
PDF_LOCK = threading.Lock()
MAX_BYTES = 20 * 1024 * 1024
LANGUAGE = os.getenv('OCR_LANGUAGES', 'eng')


def readable(text):
    chars = [c for c in text if not c.isspace()]
    return len(chars) >= 40 and sum(c.isalnum() for c in chars)/len(chars) >= .5 and text.count('\ufffd') <= max(1,len(chars)*.02)


def needs_ocr(text, image_coverage):
    return not readable(text) or image_coverage >= .35


def recognize(image):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)/'page.png'
        image.convert('RGB').save(path)
        try:
            result = subprocess.run(['tesseract',str(path),'stdout','-l',LANGUAGE,'--psm','1','tsv'],
                                    capture_output=True, text=True, timeout=45, check=True)
        except subprocess.TimeoutExpired as exc:
            raise ValueError('OCR page processing timed out. Split this PDF into smaller files.') from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError('Tesseract failed; check OCR language configuration.') from exc
    lines = {}
    confidences = []
    for row in csv.DictReader(io.StringIO(result.stdout), delimiter='\t'):
        word = (row.get('text') or '').strip()
        confidence = float(row['conf'])
        if word and confidence >= 40:
            key = (row['block_num'],row['par_num'],row['line_num'])
            lines.setdefault(key,[]).append(word)
            confidences.append(confidence)
    return '\n'.join(' '.join(words) for words in lines.values()), round(sum(confidences)/len(confidences),1) if confidences else 0


def extract_document(content):
    with PDF_LOCK:
        started = time.monotonic()
        try:
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted or not 1 <= len(reader.pages) <= 300:
                raise ValueError('Use an unencrypted PDF with 1 to 300 pages')
            document = pdfium.PdfDocument(content)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError('The PDF could not be opened. Check that it is a valid, unencrypted PDF.') from exc
        pages, warnings = [], []
        try:
            for index in range(len(document)):
                if time.monotonic()-started > 240:
                    raise ValueError('OCR document time limit reached. Split this PDF into smaller files.')
                page = document[index]
                try:
                    textpage = page.get_textpage()
                    try:
                        native = textpage.get_text_range().strip()
                    finally:
                        textpage.close()
                    width,height = page.get_size()
                    area = max(1,width*height)
                    coverage = 0
                    for obj in page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE]):
                        left,bottom,right,top = obj.get_bounds()
                        coverage += max(0,min(width,right)-max(0,left))*max(0,min(height,top)-max(0,bottom))/area
                    text, method, confidence = native, 'native', None
                    if needs_ocr(native, coverage):
                        scale = min(240/72, math.sqrt(16_000_000/area))
                        bitmap = page.render(scale=scale)
                        try:
                            image = bitmap.to_pil().copy()
                        finally:
                            bitmap.close()
                        try:
                            blank = image.convert('L').point(lambda value:0 if value>245 else 255).getbbox() is None
                            if not blank:
                                recognized, confidence = recognize(image)
                                if len(recognized.strip()) >= 20 and confidence >= 60:
                                    text = recognized
                                    # Preserve native content if the OCR missed existing text on a mixed page.
                                    if readable(native) and len(recognized)<len(native)*.8:
                                        text = native+'\n'+recognized
                                    method = 'ocr'
                                    if confidence < 80:
                                        warnings.append(f'Page {index+1}: OCR confidence is {confidence}%; review the scan for recognition errors.')
                                elif not readable(native):
                                    text, method = '', 'unreadable'
                                    warnings.append(f'Page {index+1}: no reliable OCR text; excluded from question generation.')
                                else:
                                    warnings.append(f'Page {index+1}: image OCR found no reliable text; native text retained.')
                            elif not native:
                                method = 'blank'
                        finally:
                            image.close()
                    pages.append({'page':index+1,'text':text,'method':method,'confidence':confidence})
                finally:
                    page.close()
        finally:
            document.close()
        if not any(len(p['text'].strip())>=40 for p in pages):
            raise ValueError('No readable course text found, even after OCR. Upload a clearer scan.')
        return {'pages':pages,'ocr_pages':sum(p['method']=='ocr' for p in pages),'warnings':warnings}


@app.get('/health')
def health():
    result = subprocess.run(['tesseract','--version'],capture_output=True,text=True,timeout=5,check=True)
    return {'status':'ready','engine':result.stdout.splitlines()[0],'languages':LANGUAGE}


@app.post('/extract')
async def extract(file: UploadFile = File(...)):
    content = await file.read(MAX_BYTES+1)
    if len(content)>MAX_BYTES or not content.startswith(b'%PDF-'):
        raise HTTPException(422,'Upload a PDF no larger than 20 MB')
    try:
        return await run_in_threadpool(extract_document, content)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503,str(exc)) from exc
