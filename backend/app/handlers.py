"""Eight separately deployed Lambda entry points."""
import json
from decimal import Decimal
from mangum import Mangum
from .main import app
from . import store

_api = Mangum(app, lifespan='off')

def create_quiz(event, context): return _api(event, context)
def get_quiz(event, context): return _api(event, context)
def list_public_quizzes(event, context): return _api(event, context)
def submit_quiz(event, context): return _api(event, context)
def get_submission(event, context): return _api(event, context)
def get_leaderboard(event, context): return _api(event, context)

def scoring(event, context):
    failed = []
    for record in event['Records']:
        try:
            store.score_message(json.loads(record['body']))
        except Exception:
            failed.append({'itemIdentifier':record['messageId']})
    return {'batchItemFailures':failed}

def retry_quiz_writes(event, context):
    failed = []
    for record in event['Records']:
        try:
            body = json.loads(record['body'])
            item = json.loads(body['Message'], parse_float=Decimal) if 'Message' in body else body
            store.table('Quizzes').put_item(Item=item)
        except Exception:
            failed.append({'itemIdentifier':record['messageId']})
    return {'batchItemFailures':failed}
