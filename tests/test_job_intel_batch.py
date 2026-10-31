"""Tests for batched LLM classification."""
from __future__ import annotations

import asyncio

import pytest

from leadforge.stages.job_intel_llm import (
    BatchItem,
    BatchResponse,
    classify_batch,
)


class _FakeLLM:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    async def chat_json(self, system, user, *, schema=None, **kw):
        self.calls.append((system, user, schema))
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def test_classify_batch_of_3():
    fake = _FakeLLM(BatchResponse(classifications=[
        BatchItem(
            job_id=1, level="entry", india_eligible=True,
            resume_match=0.8, reasoning="0-2 years",
        ),
        BatchItem(
            job_id=2, level="senior", india_eligible=False,
            resume_match=0.4, reasoning="Senior, onsite US",
        ),
        BatchItem(
            job_id=3, level="mid", india_eligible=True,
            resume_match=0.6, reasoning="3-5 years",
        ),
    ]))
    batch = [
        {"id": 1, "title": "ML Engineer", "location": "Bangalore", "description": "0-2 years"},
        {"id": 2, "title": "Senior ML", "location": "San Francisco", "description": ""},
        {"id": 3, "title": "ML Engineer", "location": "Remote", "description": "3-5 years"},
    ]
    result = asyncio.run(classify_batch(batch, ["python"], ["ml engineer"], llm_client=fake))
    assert len(result) == 3
    assert result[0].job_id == 1
    assert result[0].level == "entry"
    assert result[1].india_eligible is False
    assert len(fake.calls) == 1


def test_classify_batch_llm_error_propagates():
    fake = _FakeLLM(RuntimeError("rate limited"))
    with pytest.raises(RuntimeError):
        asyncio.run(classify_batch(
            [{"id": 1, "title": "X", "location": "?", "description": ""}],
            [], [], llm_client=fake,
        ))