import pytest
from pydantic import ValidationError
from app.rag import Question, check_evidence
from app.store import calculate, public_quiz


def question(**overrides):
    return {'text':'Which storage is durable?', 'options':['S3','RAM','CPU','Cache'], 'correct':0,
            'explanation':'The source says S3 provides durable object storage.',
            'chunk_id':'p1-c0','evidence':'Amazon S3 provides durable object storage.', 'page':1, **overrides}


def test_fabricated_quotes_and_source_ids_rejected():
    chunks=[{'id':'p1-c0','text':'Amazon S3 provides durable object storage.'}]
    assert check_evidence(Question(**question()), chunks)
    assert not check_evidence(Question(**question(evidence='Amazon S3 provides relational database transactions.')), chunks)
    assert not check_evidence(Question(**question(chunk_id='invented')), chunks)


def test_ambiguous_duplicate_options_and_invalid_keys_rejected():
    with pytest.raises(ValidationError): Question(**question(options=['S3','s3','CPU','RAM']))
    with pytest.raises(ValidationError): Question(**question(correct=4))


def test_answer_and_citation_leakage_prevented():
    quiz={'QuizID':'test','questions':[question()]}
    public=public_quiz(quiz)
    assert set(public['questions'][0]) == {'text','options'}


def test_time_decay_wrong_unanswered_and_expiration():
    quiz={'timer':60,'questions':[question(),question(),question()]}
    data={'answers':[0,1,-1],'times':[30,10,5],'elapsed':45}
    result=calculate(quiz,data)
    assert result['breakdown'][0]['points']==50
    assert result['breakdown'][1]['points']==0
    assert result['breakdown'][2]['points']==0
    assert result['Score']==16.67
    assert calculate(quiz,{**data,'elapsed':190})['Score']==0
    assert calculate({**quiz,'timer':0},data)['Score']==33.33
