"""Best-effort Instahyre detail fetch. Falls back to list metadata on failure."""
from __future__ import annotations

import json
import logging

from leadforge.models import Job

log = logging.getLogger(__name__)


async def enrich_instahyre_jobs(
    jobs: list[Job], *, fetcher=None, max_fetches: int = 20,
) -> list[Job]:
    """For each Instahyre job, try to fetch its detail endpoint for richer data.

    The list endpoint (`/api/v1/job_search/`) omits salary and description.
    Detail endpoint (`/api/v1/job_search/<id>`) may include them.

    Returns jobs with enriched `description` field when successful.
    """
    from leadforge.config import settings
    from leadforge.web.errors import FetchError
    from leadforge.web.fetcher import Fetcher

    router = fetcher or Fetcher(settings)
    enriched: list[Job] = []
    fetched = 0

    for job in jobs:
        if fetched >= max_fetches:
            enriched.append(job)
            continue
        # Extract numeric id from URL like /job-439641-...
        jid = None
        for seg in job.url.split("/"):
            if seg.startswith("job-"):
                try:
                    jid = int(seg.split("-")[1])
                except (IndexError, ValueError):
                    pass
                break
        if not jid:
            enriched.append(job)
            continue

        try:
            resp = await router.fetch(
                f"https://www.instahyre.com/api/v1/job_search/{jid}",
                min_tier=1, max_tier=1, purpose="job",
            )
            body = resp.html or ""
            fetched += 1
            try:
                detail = json.loads(body)
            except json.JSONDecodeError:
                enriched.append(job)
                continue
            # Extract description/salary if present
            desc_parts = [job.description] if job.description else []
            detail_desc = detail.get("description") or detail.get("job_description")
            if detail_desc:
                desc_parts.append(str(detail_desc))
            salary = detail.get("salary") or detail.get("compensation")
            if salary:
                desc_parts.insert(0, f"Salary: {salary}. ")
            if len(desc_parts) > 1:
                enriched.append(
                    job.model_copy(update={"description": "\n\n".join(desc_parts)[:8000]})
                )
            else:
                enriched.append(job)
        except FetchError:
            enriched.append(job)
        except Exception as exc:  # noqa: BLE001
            log.warning("Instahyre detail fetch failed for %s: %s", jid, exc)
            enriched.append(job)

    return enriched