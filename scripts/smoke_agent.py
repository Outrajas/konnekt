"""Tight-budget live smoke: 2 pages max, 8K token ceiling."""
import asyncio
import sys
from pathlib import Path

_repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo / "src"))

from leadforge.sources.people_agent import find_people_agentic


async def main():
    result = await find_people_agentic(
        company="Linear",
        start_url="https://linear.app/about",
        goal="find engineering managers or talent acquisition",
        max_pages=2,
        max_tokens=8000,
    )
    print(f"pages_fetched: {result.pages_fetched}")
    print(f"people_found:  {len(result.people)}")
    print(f"tokens_used:   {result.tokens_used}")
    print(f"stop_reason:   {result.stop_reason}")
    print()
    for p in result.people[:6]:
        print(f"  {p.name!r} - {p.title!r}  [{p.source}]")
    print()
    print("page decisions:")
    for page in result.pages:
        print(f"  {page.url}")
        print(f"    people={page.people_found} tokens={page.tokens_used}")
        print(f"    decision: {page.llm_decision[:120]}")


asyncio.run(main())