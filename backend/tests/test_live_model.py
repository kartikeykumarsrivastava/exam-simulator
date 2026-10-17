"""Explicit live Ollama check; deliberately independent of LocalStack availability."""
import os
import pytest
from app import rag

pytestmark=pytest.mark.skipif(not os.getenv('LIVE_MODEL_TESTS'),reason='Set LIVE_MODEL_TESTS=1 to run actual local AI inference')


def test_live_embeddings_and_grounded_graph(monkeypatch):
    text=('In a first-in, first-out queue, the first item inserted is the first item removed. '
          'Enqueue adds an item to the back of the queue. Dequeue removes an item from the front. '
          'A stack follows last-in, first-out ordering. Push adds an item to the top of the stack, '
          'and pop removes the most recently added item from the top. '
          'A queue and a stack are different because their removal orders differ.')
    vector=rag.embeddings().embed_query('first-in first-out queues')
    assert len(vector)>100
    monkeypatch.setattr(rag,'retrieve',lambda *_:[{'id':'p1-c0','page':1,'text':text}])
    questions=rag.generate('testcourse','first-in first-out queue removal order',1,'Beginner')
    assert len(questions)==1
    assert rag.check_evidence(rag.Question.model_validate(questions[0]),[{'id':'p1-c0','text':text}])
    assert questions[0]['page']==1
