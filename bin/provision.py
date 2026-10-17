"""Provision LocalStack through AWS CLI. Run in the API deployment container."""
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time
import zipfile

REGION = 'us-east-1'
ENDPOINT = os.getenv('AWS_ENDPOINT_URL', 'http://localhost:4566')
os.environ.update(AWS_ACCESS_KEY_ID='test', AWS_SECRET_ACCESS_KEY='test', AWS_DEFAULT_REGION=REGION, AWS_MAX_ATTEMPTS='1', AWS_EC2_METADATA_DISABLED='true')
ROOT = Path(__file__).resolve().parents[1]
ART = ROOT/'artifacts'
ART.mkdir(exist_ok=True)


def aws(service, operation, **kwargs):
    args = ['aws', '--endpoint-url', ENDPOINT, '--region', REGION, '--cli-connect-timeout', '3', '--cli-read-timeout', '30', '--output', 'json', service, operation]
    for key,value in kwargs.items():
        flag = re.sub(r'(?<!^)(?=[A-Z][a-z])|(?<=[a-z0-9])(?=[A-Z])', '-', key).replace('_','-').lower()
        args.append('--'+flag)
        if isinstance(value,bool):
            if not value: args[-1] = '--no-'+key.replace('_','-')
        else:
            args.append(json.dumps(value) if isinstance(value,(dict,list)) else str(value))
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f'{service} {operation}: {result.stderr.strip()}')
    return json.loads(result.stdout) if result.stdout.strip() else {}


def ensure(service, operation, **kwargs):
    try: return aws(service,operation,**kwargs)
    except RuntimeError as exc:
        if any(t in str(exc) for t in ['AlreadyExists','ResourceInUseException','BucketAlreadyOwnedByYou','EntityAlreadyExists','ResourceConflictException','ConflictException']): return {}
        raise


def role(name, principal, statements):
    ensure('iam','create-role',RoleName=name,AssumeRolePolicyDocument={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Service':principal},'Action':'sts:AssumeRole'}]})
    aws('iam','put-role-policy',RoleName=name,PolicyName='ScopedAccess',PolicyDocument={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':a,'Resource':r} for a,r in statements]})
    return f'arn:aws:iam::000000000000:role/{name}'


def provision():
    limitations=[]
    for attempt in range(10):
        try: aws('sts','get-caller-identity');break
        except RuntimeError:
            if attempt==9: raise RuntimeError('LocalStack unavailable. Check docker compose logs localstack and configure LOCALSTACK_AUTH_TOKEN in .env.')
            time.sleep(2)
    # 1. Database tables; sparse score GSI excludes pending submissions.
    ensure('dynamodb','create-table',TableName='Quizzes',KeySchema=[{'AttributeName':'QuizID','KeyType':'HASH'}],AttributeDefinitions=[{'AttributeName':'QuizID','AttributeType':'S'}],BillingMode='PAY_PER_REQUEST')
    ensure('dynamodb','create-table',TableName='UserSubmissions',KeySchema=[{'AttributeName':'SubmissionID','KeyType':'HASH'}],AttributeDefinitions=[{'AttributeName':'SubmissionID','AttributeType':'S'},{'AttributeName':'QuizID','AttributeType':'S'},{'AttributeName':'Score','AttributeType':'N'}],BillingMode='PAY_PER_REQUEST',GlobalSecondaryIndexes=[{'IndexName':'QuizScoreIndex','KeySchema':[{'AttributeName':'QuizID','KeyType':'HASH'},{'AttributeName':'Score','KeyType':'RANGE'}],'Projection':{'ProjectionType':'ALL'}}])
    for bucket in ['webapp','exam-courses']:
        ensure('s3api','create-bucket',Bucket=bucket)
    # 2. Messaging, redrive and retries.
    urls={}
    arns={}
    for name in ['QuizSubmissionDLQ','QuizSubmissionQueue','QuizzesWriteFailuresQueue']:
        urls[name]=aws('sqs','create-queue',QueueName=name,Attributes={'VisibilityTimeout':'180'})['QueueUrl']
        arns[name]=aws('sqs','get-queue-attributes',QueueUrl=urls[name],AttributeNames=['QueueArn'])['Attributes']['QueueArn']
    aws('sqs','set-queue-attributes',QueueUrl=urls['QuizSubmissionQueue'],Attributes={'RedrivePolicy':json.dumps({'deadLetterTargetArn':arns['QuizSubmissionDLQ'],'maxReceiveCount':1})})
    topics={n:aws('sns','create-topic',Name=n)['TopicArn'] for n in ['QuizzesWriteFailures','DLQAlarmTopic']}
    aws('sns','subscribe',TopicArn=topics['QuizzesWriteFailures'],Protocol='sqs',Endpoint=arns['QuizzesWriteFailuresQueue'])
    aws('sqs','set-queue-attributes',QueueUrl=urls['QuizzesWriteFailuresQueue'],Attributes={'Policy':json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Service':'sns.amazonaws.com'},'Action':'sqs:SendMessage','Resource':arns['QuizzesWriteFailuresQueue'],'Condition':{'ArnEquals':{'aws:SourceArn':topics['QuizzesWriteFailures']}}}]})})
    # 3. Email state machine and dedicated role.
    sender='results@exam.local'
    aws('ses','verify-email-identity',EmailAddress=sender)
    sfrole=role('EmailWorkflowRole','states.amazonaws.com',[(['ses:SendEmail'],f'arn:aws:ses:{REGION}:000000000000:identity/{sender}')])
    definition={'StartAt':'HasEmail','States':{'HasEmail':{'Type':'Choice','Choices':[{'Variable':'$.email','IsPresent':True,'Next':'SendEmail'}],'Default':'Done'},'SendEmail':{'Type':'Task','Resource':'arn:aws:states:::aws-sdk:ses:sendEmail','Parameters':{'Source':sender,'Destination':{'ToAddresses.$':'States.Array($.email)'},'Message':{'Subject':{'Data':'Your Exam Studio result'},'Body':{'Html':{'Data.$':"States.Format('<h1>Your score: {}</h1><p>Keep learning with Exam Studio.</p>', $.score)"}}}},'Catch':[{'ErrorEquals':['States.ALL'],'ResultPath':'$.error','Next':'Failed'}],'Next':'Done'},'Done':{'Type':'Succeed'},'Failed':{'Type':'Fail','Error':'EmailDeliveryFailed','Cause':'SES delivery failed; inspect execution error details'}}}
    sfarn=f'arn:aws:states:{REGION}:000000000000:stateMachine:SendEmailStateMachine'
    ensure('stepfunctions','create-state-machine',Name='SendEmailStateMachine',Definition=json.dumps(definition),RoleArn=sfrole)
    aws('stepfunctions','update-state-machine',StateMachineArn=sfarn,Definition=json.dumps(definition),RoleArn=sfrole)
    # 4. Package Python dependencies for Linux Lambda (container Python matches runtime).
    package=ART/'lambda'
    package.mkdir(exist_ok=True)
    subprocess.run([sys.executable,'-m','pip','install','--target',str(package),'-r',str(ROOT/'backend/requirements.txt'),'--quiet'],check=True)
    import shutil
    shutil.copytree(ROOT/'backend/app',package/'app',dirs_exist_ok=True)
    archive=ART/'lambda.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for path in package.rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts: z.write(path,path.relative_to(package))
    aws('s3api','put-object',Bucket='exam-courses',Key='deploy/lambda.zip',Body='fileb://'+str(archive))
    quizarn=f'arn:aws:dynamodb:{REGION}:000000000000:table/Quizzes'
    subarn=f'arn:aws:dynamodb:{REGION}:000000000000:table/UserSubmissions'
    specs={
      'CreateQuizFunction':('create_quiz',[(['dynamodb:PutItem'],quizarn),(['sns:CreateTopic','sns:Publish'],topics['QuizzesWriteFailures']),(['s3:GetObject','s3:PutObject'],'arn:aws:s3:::exam-courses/*')]),
      'GetQuizFunction':('get_quiz',[(['dynamodb:GetItem'],quizarn)]),
      'ListPublicQuizzesFunction':('list_public_quizzes',[(['dynamodb:Scan'],quizarn)]),
      'SubmitQuizFunction':('submit_quiz',[(['dynamodb:GetItem'],[quizarn,subarn]),(['dynamodb:PutItem','dynamodb:UpdateItem'],subarn),(['sqs:GetQueueUrl','sqs:SendMessage'],arns['QuizSubmissionQueue'])]),
      'ScoringFunction':('scoring',[(['dynamodb:GetItem'],quizarn),(['dynamodb:PutItem'],subarn),(['sqs:ReceiveMessage','sqs:DeleteMessage','sqs:GetQueueAttributes'],arns['QuizSubmissionQueue']),(['states:StartExecution'],sfarn)]),
      'GetSubmissionFunction':('get_submission',[(['dynamodb:GetItem'],subarn)]),
      'GetLeaderboardFunction':('get_leaderboard',[(['dynamodb:Query'],subarn+'/index/QuizScoreIndex')]),
      'RetryQuizzesWritesFunction':('retry_quiz_writes',[(['dynamodb:PutItem'],quizarn),(['sqs:ReceiveMessage','sqs:DeleteMessage','sqs:GetQueueAttributes'],arns['QuizzesWriteFailuresQueue'])])}
    environment={'AWS_ENDPOINT_URL':'http://localstack:4566','OLLAMA_BASE_URL':'http://ollama:11434','OCR_BASE_URL':'http://ocr:8001','DATA_DIR':'/tmp/courses','EMAIL_STATE_MACHINE_ARN':sfarn}
    for name,(handler,permissions) in specs.items():
        rolearn=role(name+'Role','lambda.amazonaws.com',permissions+[(['logs:CreateLogGroup','logs:CreateLogStream','logs:PutLogEvents'],f'arn:aws:logs:{REGION}:000000000000:log-group:/aws/lambda/{name}:*')])
        try:
            aws('lambda','get-function',FunctionName=name)
            aws('lambda','update-function-code',FunctionName=name,S3Bucket='exam-courses',S3Key='deploy/lambda.zip')
            subprocess.run(['aws','--endpoint-url',ENDPOINT,'lambda','wait','function-updated-v2','--function-name',name],check=True)
            aws('lambda','update-function-configuration',FunctionName=name,Environment={'Variables':environment},Timeout=120,MemorySize=1024,Role=rolearn)
        except RuntimeError as exc:
            if 'ResourceNotFoundException' not in str(exc): raise
            aws('lambda','create-function',FunctionName=name,Runtime='python3.12',Handler='app.handlers.'+handler,Role=rolearn,Code={'S3Bucket':'exam-courses','S3Key':'deploy/lambda.zip'},Environment={'Variables':environment},Timeout=120,MemorySize=1024)
        subprocess.run(['aws','--endpoint-url',ENDPOINT,'lambda','wait','function-active-v2','--function-name',name],check=True)
    mappings=aws('lambda','list-event-source-mappings')['EventSourceMappings']
    for fn,qn in [('ScoringFunction','QuizSubmissionQueue'),('RetryQuizzesWritesFunction','QuizzesWriteFailuresQueue')]:
        if not any(m['FunctionArn'].endswith(':'+fn) and m['EventSourceArn']==arns[qn] for m in mappings):
            aws('lambda','create-event-source-mapping',FunctionName=fn,EventSourceArn=arns[qn],BatchSize=10,FunctionResponseTypes=['ReportBatchItemFailures'])
    # 5. REST API, six original resources + leaderboard + PDF/attempt extensions.
    existing=aws('apigateway','get-rest-apis')['items']
    api=next((a for a in existing if a['name']=='QuizAPI'),None) or aws('apigateway','create-rest-api',Name='QuizAPI',BinaryMediaTypes=['multipart/form-data'])
    api_id=api['id']
    resources=aws('apigateway','get-resources',RestApiId=api_id)['items']
    root=next(r['id'] for r in resources if r['path']=='/')
    routes=[('createquiz','POST','CreateQuizFunction'),('getquiz','GET','GetQuizFunction'),('listquizzes','GET','ListPublicQuizzesFunction'),('submitquiz','POST','SubmitQuizFunction'),('getsubmission','GET','GetSubmissionFunction'),('getleaderboard','GET','GetLeaderboardFunction'),('courses','POST','CreateQuizFunction'),('attempts','ANY','SubmitQuizFunction')]
    for path,method,fn in routes:
        resource=next((r for r in resources if r['path']=='/'+path),None) or aws('apigateway','create-resource',RestApiId=api_id,ParentId=root,PathPart=path)
        if path=='attempts':
            resource=next((r for r in resources if r['path']=='/attempts/{proxy+}'),None) or aws('apigateway','create-resource',RestApiId=api_id,ParentId=resource['id'],PathPart='{proxy+}')
        rid=resource['id']
        aws('apigateway','put-method',RestApiId=api_id,ResourceId=rid,HttpMethod=method,AuthorizationType='NONE')
        aws('apigateway','put-integration',RestApiId=api_id,ResourceId=rid,HttpMethod=method,Type='AWS_PROXY',IntegrationHttpMethod='POST',Uri=f'arn:aws:apigateway:{REGION}:lambda:path/2015-03-31/functions/arn:aws:lambda:{REGION}:000000000000:function:{fn}/invocations')
        ensure('lambda','add-permission',FunctionName=fn,StatementId='gateway-'+path,Action='lambda:InvokeFunction',Principal='apigateway.amazonaws.com',SourceArn=f'arn:aws:execute-api:{REGION}:000000000000:{api_id}/*')
        aws('apigateway','put-method',RestApiId=api_id,ResourceId=rid,HttpMethod='OPTIONS',AuthorizationType='NONE')
        aws('apigateway','put-integration',RestApiId=api_id,ResourceId=rid,HttpMethod='OPTIONS',Type='MOCK',RequestTemplates={'application/json':'{"statusCode":200}'})
        headers={'method.response.header.Access-Control-Allow-Origin':False,'method.response.header.Access-Control-Allow-Headers':False,'method.response.header.Access-Control-Allow-Methods':False}
        aws('apigateway','put-method-response',RestApiId=api_id,ResourceId=rid,HttpMethod='OPTIONS',StatusCode='200',ResponseParameters=headers)
        aws('apigateway','put-integration-response',RestApiId=api_id,ResourceId=rid,HttpMethod='OPTIONS',StatusCode='200',ResponseParameters={k:"'*'" for k in headers})
    aws('apigateway','create-deployment',RestApiId=api_id,StageName='prod')
    # 6. DLQ alarm pipe; fail explicitly if installed LocalStack edition lacks support.
    piperole=role('DLQPipeRole','pipes.amazonaws.com',[(['sqs:ReceiveMessage','sqs:DeleteMessage','sqs:GetQueueAttributes'],arns['QuizSubmissionDLQ']),(['sns:Publish'],topics['DLQAlarmTopic'])])
    try:
        ensure('pipes','create-pipe',Name='DLQAlertPipe',RoleArn=piperole,Source=arns['QuizSubmissionDLQ'],Target=topics['DLQAlarmTopic'],SourceParameters={'SqsQueueParameters':{'BatchSize':1}})
    except RuntimeError as exc:
        limitations.append(str(exc))
    alert=os.getenv('ALERT_EMAIL')
    if alert:
        aws('ses','verify-email-identity',EmailAddress=alert)
        aws('sns','subscribe',TopicArn=topics['DLQAlarmTopic'],Protocol='email',Endpoint=alert)
    # 7. Static site and CloudFront. Build output is produced by frontend Docker image.
    dist=ROOT/'frontend/dist'
    if not (dist/'index.html').exists():
        raise RuntimeError('frontend/dist missing: build frontend before provisioning')
    url=f'http://{api_id}.execute-api.localhost.localstack.cloud:4566/prod'
    (dist/'config.js').write_text('window.EXAM_API_URL='+json.dumps(url)+';window.EXAM_AI_API_URL="http://localhost:8000";')
    html=(dist/'index.html').read_text()
    if 'config.js' not in html:
        (dist/'index.html').write_text(html.replace('<head>','<head><script src="/config.js"></script>'))
    subprocess.run(['aws','--endpoint-url',ENDPOINT,'s3','sync',str(dist),'s3://webapp','--delete'],check=True)
    aws('s3api','put-bucket-website',Bucket='webapp',WebsiteConfiguration={'IndexDocument':{'Suffix':'index.html'},'ErrorDocument':{'Key':'index.html'}})
    aws('s3api','put-bucket-policy',Bucket='webapp',Policy=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':'*','Action':'s3:GetObject','Resource':'arn:aws:s3:::webapp/*'}]}))
    config={'CallerReference':'exam-studio-v1','Comment':'Exam Studio local distribution','Enabled':True,'DefaultRootObject':'index.html','Origins':{'Quantity':1,'Items':[{'Id':'webapp','DomainName':'webapp.s3.localhost.localstack.cloud','CustomOriginConfig':{'HTTPPort':4566,'HTTPSPort':443,'OriginProtocolPolicy':'http-only'}}]},'DefaultCacheBehavior':{'TargetOriginId':'webapp','ViewerProtocolPolicy':'allow-all','ForwardedValues':{'QueryString':False,'Cookies':{'Forward':'none'}},'MinTTL':0},'CustomErrorResponses':{'Quantity':1,'Items':[{'ErrorCode':404,'ResponsePagePath':'/index.html','ResponseCode':'200','ErrorCachingMinTTL':0}]}}
    cloud={}
    try:
        distributions=aws('cloudfront','list-distributions').get('DistributionList',{}).get('Items',[])
        cloud=next((d for d in distributions if d.get('Comment')=='Exam Studio local distribution'),None)
        if not cloud: cloud=aws('cloudfront','create-distribution',DistributionConfig=config)['Distribution']
    except RuntimeError as exc:
        limitations.append(str(exc))
    deployment={'api_url':url,'s3_url':'http://webapp.s3-website.localhost.localstack.cloud:4566','cloudfront_domain':(cloud or {}).get('DomainName'),'local_app':'http://localhost:3000','email_state_machine_arn':sfarn,'limitations':limitations,'complete':not limitations}
    (ART/'deployment.json').write_text(json.dumps(deployment,indent=2))
    print(json.dumps(deployment,indent=2))
    if limitations:
        raise RuntimeError('Core services provisioned, but required optional-plan services failed. See artifacts/deployment.json; full deployment is incomplete.')


if __name__=='__main__':
    provision()
