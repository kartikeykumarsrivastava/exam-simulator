"""Run after deployment: API_URL=http://localhost:8000 pytest tests/test_integration.py.
These tests use a controlled, source-cited fixture to isolate AWS infrastructure from model quality.
"""
import os
import time
import uuid
import boto3
import pytest
import requests

pytestmark=pytest.mark.skipif(not os.getenv('RUN_INTEGRATION'),reason='Requires deployed LocalStack; set RUN_INTEGRATION=1')
API=os.getenv('API_URL','http://localhost:8000')


@pytest.fixture
def quiz_id():
    qid='integration-'+uuid.uuid4().hex[:10]
    db=boto3.resource('dynamodb',endpoint_url=os.getenv('AWS_ENDPOINT_URL','http://localhost:4566'),region_name='us-east-1',aws_access_key_id='test',aws_secret_access_key='test')
    db.Table('Quizzes').put_item(Item={'QuizID':qid,'title':'Integration fixture','visibility':'Public','timer':0,'questions':[{'text':'Which service stores objects?', 'options':['S3','SQS','SNS','SES'],'correct':0,'explanation':'S3 stores objects.','evidence':'Amazon S3 is an object storage service.','chunk_id':'p1-c0','page':1}]})
    yield qid
    db.Table('Quizzes').delete_item(Key={'QuizID':qid})
    rows=db.Table('UserSubmissions').scan()['Items']
    for row in rows:
        if row.get('QuizID')==qid:
            db.Table('UserSubmissions').delete_item(Key={'SubmissionID':row['SubmissionID']})


def test_retrieval_async_scoring_and_leaderboard(quiz_id):
    r=requests.get(API+'/getquiz',params={'quiz_id':quiz_id},timeout=15)
    assert r.status_code==200
    assert 'correct' not in r.json()['questions'][0]
    attempt=requests.post(API+'/attempts/'+quiz_id,json={},timeout=15).json()['attempt_id']
    r=requests.post(API+'/submitquiz',json={'quiz_id':quiz_id,'attempt_id':attempt,'name':'Integration learner','answers':[0],'times':[0]},timeout=15)
    assert r.status_code==202
    for _ in range(45):
        result=requests.get(API+'/getsubmission',params={'submission_id':r.json()['submission_id']},timeout=15).json()
        if result['status']=='scored':break
        time.sleep(2)
    assert result['status']=='scored'
    assert result['Score']==100
    for _ in range(10):
        board=requests.get(API+'/getleaderboard',params={'quiz_id':quiz_id},timeout=15).json()
        if board:break
        time.sleep(1)
    assert board[0]['score']==100


def test_invalid_input():
    assert requests.get(API+'/getquiz',params={'quiz_id':'missing'},timeout=15).status_code==404
    assert requests.post(API+'/createquiz',json={},timeout=15).status_code==422
    assert requests.post(API+'/submitquiz',json={},timeout=15).status_code==422


def test_ai_creation_from_ingested_course():
    course_id=os.getenv('TEST_COURSE_ID')
    if not course_id:pytest.skip('Set TEST_COURSE_ID and TEST_TOPIC for real model generation test')
    r=requests.post(API+'/createquiz',json={'course_id':course_id,'title':'Generated integration exam','topic':os.getenv('TEST_TOPIC','course fundamentals'),'count':1,'timer':0},timeout=600)
    assert r.status_code==200,r.text
    q=requests.get(API+'/getquiz',params={'quiz_id':r.json()['id']},timeout=15)
    assert q.status_code==200
    assert len(q.json()['questions'])==1
