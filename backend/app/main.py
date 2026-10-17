import os
import tempfile
from pathlib import Path
from typing import Literal
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from botocore.exceptions import BotoCoreError, ClientError
from starlette.concurrency import run_in_threadpool
from . import rag, store
from .extraction import OCRUnavailable

app = FastAPI(title='Exam Studio API')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])


class BuildRequest(BaseModel):
    course_id: str = Field(pattern=r'^[a-f0-9]{20}$')
    title: str = Field(min_length=3, max_length=120)
    topic: str = Field(min_length=3, max_length=200)
    count: int = Field(default=5, ge=1, le=15)
    difficulty: Literal['Beginner','Intermediate','Advanced'] = 'Intermediate'
    visibility: Literal['Public','Private'] = 'Public'
    timer: int = Field(default=0, ge=0, le=600)


class Submission(BaseModel):
    quiz_id: str
    attempt_id: str
    name: str = Field(min_length=1, max_length=60)
    email: str = Field(default='', max_length=254, pattern=r'^$|^[^\s@]+@[^\s@]+\.[^\s@]+$')
    answers: list[int]
    times: list[int] = Field(min_length=1)


@app.exception_handler(ValueError)
async def bad_input(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=422, content={'detail':str(exc)})


async def infrastructure_unavailable(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=503, content={'detail':'LocalStack storage or messaging is unavailable. Check service status and finish infrastructure deployment.'})


app.add_exception_handler(BotoCoreError, infrastructure_unavailable)
app.add_exception_handler(ClientError, infrastructure_unavailable)


@app.get('/health')
def health():
    status = {}
    for name, check in [('database', lambda:store.client('dynamodb').list_tables()),
                        ('model', lambda:__import__('httpx').get(rag.BASE+'/api/tags', timeout=3).raise_for_status()),
                        ('ocr', lambda:__import__('httpx').get(os.getenv('OCR_BASE_URL','http://localhost:8001')+'/health', timeout=3).raise_for_status())]:
        try:
            check()
            status[name] = 'connected'
        except Exception:
            status[name] = 'unavailable'
    return status


@app.post('/courses')
async def upload_course(files: list[UploadFile] = File(default=[]), file: UploadFile | None = File(default=None)):
    documents = [*files, *([file] if file else [])]
    if not 1 <= len(documents) <= 10:
        raise HTTPException(422, 'Upload between 1 and 10 PDFs')
    with tempfile.TemporaryDirectory() as directory:
        paths = []
        total = 0
        for index, document in enumerate(documents):
            content = await document.read(20*1024*1024+1)
            total += len(content)
            if len(content) > 20*1024*1024 or not content.startswith(b'%PDF-'):
                raise HTTPException(422, 'Each file must be a PDF no larger than 20 MB')
            if total > 50*1024*1024:
                raise HTTPException(422, 'Combined upload must not exceed 50 MB')
            path = Path(directory)/f'course-{index}.pdf'
            path.write_bytes(content)
            paths.append((path, (document.filename or f'Course {index+1}.pdf')[:200]))
        try:
            return await run_in_threadpool(rag.ingest_many, paths)
        except ValueError:
            raise
        except OCRUnavailable as exc:
            raise HTTPException(503,str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, 'PDF ingestion failed. Check PDF readability and the Ollama embedding model.') from exc


@app.post('/createquiz')
def create_quiz(data: BuildRequest):
    try:
        questions = rag.generate(data.course_id, data.topic, data.count, data.difficulty)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, 'AI generation unavailable. Check that Ollama models are loaded.') from exc
    return store.create_quiz(data.model_dump() | {'questions':questions})


@app.get('/getquiz')
def get_quiz(quiz_id: str):
    item = store.get_quiz(quiz_id)
    if not item:
        raise HTTPException(404, 'Quiz not found')
    return store.public_quiz(item)


@app.get('/listquizzes')
def list_quizzes():
    return store.list_quizzes()


@app.post('/attempts/{quiz_id}')
def attempt(quiz_id: str):
    return store.begin_attempt(quiz_id)


@app.post('/submitquiz', status_code=202)
def submit(data: Submission):
    return store.submit(data.model_dump())


@app.get('/getsubmission')
def get_submission(submission_id: str):
    item = store.table('UserSubmissions').get_item(Key={'SubmissionID':submission_id}, ConsistentRead=True).get('Item')
    if not item:
        raise HTTPException(404, 'Submission not found')
    return item


@app.get('/getleaderboard')
def leaderboard(quiz_id: str, limit: int = 10):
    return store.leaderboard(quiz_id, min(100, max(1,limit)))
