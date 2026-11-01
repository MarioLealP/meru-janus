"""Asking Shikigami whether a request file is valid on main (D96): the format it runs on decides."""

import httpx2

from janus.models import Submission, Verdict


async def validate(http: httpx2.AsyncClient, url: str, submission: Submission) -> Verdict:
    response = await http.post(url, json=submission.model_dump())
    response.raise_for_status()
    return Verdict.model_validate_json(response.content)
