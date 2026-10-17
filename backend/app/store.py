import json
import os
import random
import time
import uuid
from decimal import Decimal
import boto3
from botocore.exceptions import ClientError
from botocore.config import Config


def client(service):
    return boto3.client(service, region_name=os.getenv('AWS_DEFAULT_REGION', 'us-east-1'),
                        config=Config(connect_timeout=3, read_timeout=15, retries={'max_attempts':1}),
                        endpoint_url=os.getenv('AWS_ENDPOINT_URL'),
                        aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID', 'test'),
                        aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY', 'test'))


def table(name):
    return boto3.resource('dynamodb', region_name=os.getenv('AWS_DEFAULT_REGION', 'us-east-1'),
                          config=Config(connect_timeout=3, read_timeout=15, retries={'max_attempts':1}),
                          endpoint_url=os.getenv('AWS_ENDPOINT_URL'),
                          aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID', 'test'),
                          aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY', 'test')).Table(name)


def queue(name):
    return client('sqs').get_queue_url(QueueName=name)['QueueUrl']


def create_quiz(data):
    quiz_id = f"{random.choice(['clever','bright','calm','bold'])}-{random.choice(['fox','owl','eagle','otter'])}-{uuid.uuid4().hex[:8]}"
    item = {**data, 'QuizID': quiz_id, 'created_at': int(time.time())}
    try:
        table('Quizzes').put_item(Item=item, ConditionExpression='attribute_not_exists(QuizID)')
        return {'id': quiz_id, 'status': 'ready'}
    except ClientError:
        arn = client('sns').create_topic(Name='QuizzesWriteFailures')['TopicArn']
        client('sns').publish(TopicArn=arn, Message=json.dumps(item))
        return {'id': quiz_id, 'status': 'pending_retry'}


def get_quiz(quiz_id):
    return table('Quizzes').get_item(Key={'QuizID': quiz_id}, ConsistentRead=True).get('Item')


def public_quiz(item):
    return {k:v for k,v in item.items() if k != 'questions'} | {
        'questions': [{'text':q['text'], 'options':q['options']} for q in item['questions']]}


def list_quizzes():
    items, start = [], None
    while True:
        args = {'FilterExpression': '#v = :public', 'ExpressionAttributeNames': {'#v':'visibility'},
                'ExpressionAttributeValues': {':public':'Public'}}
        if start:
            args['ExclusiveStartKey'] = start
        result = table('Quizzes').scan(**args)
        items.extend({k:v for k,v in q.items() if k != 'questions'} | {'question_count': len(q['questions'])} for q in result['Items'])
        start = result.get('LastEvaluatedKey')
        if not start:
            return items


def begin_attempt(quiz_id):
    if not get_quiz(quiz_id):
        raise ValueError('Quiz not found')
    attempt_id = uuid.uuid4().hex
    table('UserSubmissions').put_item(Item={'SubmissionID': attempt_id, 'QuizID': quiz_id,
        'status': 'started', 'started_at': int(time.time())})
    return {'attempt_id': attempt_id}


def submit(data):
    quiz = get_quiz(data['quiz_id'])
    if not quiz:
        raise ValueError('Quiz not found')
    attempt = table('UserSubmissions').get_item(Key={'SubmissionID': data['attempt_id']}, ConsistentRead=True).get('Item')
    if not attempt or attempt['QuizID'] != data['quiz_id']:
        raise ValueError('Invalid attempt')
    if len(data['answers']) != len(quiz['questions']) or len(data['times']) != len(quiz['questions']):
        raise ValueError('Answer and timing counts must match the quiz')
    if any(a < -1 or a > 3 for a in data['answers']) or any(t < 0 for t in data['times']):
        raise ValueError('Invalid answers or timings')
    elapsed = max(0, int(time.time()) - int(attempt['started_at']))
    if sum(data['times']) > elapsed + 5:
        raise ValueError('Reported time exceeds server attempt duration')
    payload = {**data, 'elapsed': elapsed, 'started_at': int(attempt['started_at'])}
    # Persist before enqueue; re-sending is safe because scoring is idempotent.
    table('UserSubmissions').update_item(Key={'SubmissionID': data['attempt_id']},
        UpdateExpression='SET #s = :pending', ConditionExpression='#s IN (:started, :pending)',
        ExpressionAttributeNames={'#s':'status'}, ExpressionAttributeValues={':pending':'pending', ':started':'started'})
    client('sqs').send_message(QueueUrl=queue('QuizSubmissionQueue'), MessageBody=json.dumps(payload))
    return {'submission_id': data['attempt_id'], 'status': 'pending'}


def calculate(quiz, data):
    timer = int(quiz.get('timer', 0))
    expired = timer > 0 and data['elapsed'] > timer * len(quiz['questions']) + 5
    breakdown = []
    for i,q in enumerate(quiz['questions']):
        correct = data['answers'][i] == q['correct']
        points = round(100 * max(0, 1-data['times'][i]/timer), 2) if timer else 100
        if not correct or expired:
            points = 0
        breakdown.append({'question':q['text'], 'options':q['options'], 'answer':data['answers'][i],
            'correct':q['correct'], 'points':points, 'explanation':q['explanation'],
            'evidence':q['evidence'], 'page':q.get('page'), 'source':q.get('source', 'Course PDF'), 'chunk_id':q['chunk_id']})
    return {'Score': round(sum(q['points'] for q in breakdown)/len(breakdown), 2),
            'correct_count':sum(data['answers'][i] == q['correct'] for i,q in enumerate(quiz['questions'])),
            'breakdown':breakdown, 'expired':expired}


def score_message(data):
    quiz = get_quiz(data['quiz_id'])
    if not quiz:
        raise ValueError('Quiz missing during scoring')
    result = calculate(quiz, data)
    item = {'SubmissionID': data['attempt_id'], 'QuizID':data['quiz_id'], 'name':data['name'],
            'status':'scored', 'completed_at':int(time.time()), **result}
    try:
        table('UserSubmissions').put_item(Item=json.loads(json.dumps(item), parse_float=Decimal),
            ConditionExpression='attribute_not_exists(Score)')
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
    if data.get('email'):
        arn = os.getenv('EMAIL_STATE_MACHINE_ARN')
        if not arn:
            raise ValueError('Email workflow is not configured')
        try:
            client('stepfunctions').start_execution(stateMachineArn=arn, name=data['attempt_id'],
                input=json.dumps({'email':data['email'], 'score':str(result['Score'])}))
        except ClientError as exc:
            if exc.response['Error']['Code'] != 'ExecutionAlreadyExists':
                raise
    return item


def leaderboard(quiz_id, limit=10):
    result = table('UserSubmissions').query(IndexName='QuizScoreIndex',
        KeyConditionExpression='QuizID = :id', ExpressionAttributeValues={':id':quiz_id},
        ScanIndexForward=False, Limit=limit)
    return [{'name':r['name'], 'score':r['Score'], 'correct_count':r['correct_count']} for r in result['Items']]
