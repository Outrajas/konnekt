"""Offline page loader for recorded walk fixtures."""

from __future__ import annotations

import json
from pathlib import Path

from leadforge.stages.walk import WalkPage


class ReplayFetcher:
    """Serve pages only from an explicit fixture manifest; never access the network."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        manifest_path = self.directory / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid replay manifest at {manifest_path}: {exc}") from exc
        pages = manifest.get("pages") if isinstance(manifest, dict) else None
        if not isinstance(pages, dict) or not pages:
            raise ValueError("Replay manifest must contain a non-empty 'pages' mapping")
        seeds = manifest.get("seeds", [])
        if not isinstance(seeds, list) or any(not isinstance(item, str) for item in seeds):
            raise ValueError("Replay manifest 'seeds' must be a list of URLs")
        self.seeds = seeds
        self.pages: dict[str, Path] = {}
        for url, relative in pages.items():
            if not isinstance(url, str) or not isinstance(relative, str):
                raise ValueError("Replay URL and fixture paths must be strings")
            fixture = (self.directory / relative).resolve()
            if self.directory not in fixture.parents or not fixture.is_file():
                raise ValueError(f"Replay fixture is missing or outside its directory: {relative}")
            self.pages[url] = fixture

    async def __call__(self, url: str) -> WalkPage:
        fixture = self.pages.get(url)
        if fixture is None:
            return WalkPage(url=url, html="", status=404)
        return WalkPage(url=url, html=fixture.read_text(encoding="utf-8"))
