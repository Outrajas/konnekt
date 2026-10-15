import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from leadforge import clock
from leadforge.config import Settings
from leadforge.db import connect
from leadforge.web import FetchResult

TRACKING_PREFIXES = ("utm_",)


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    query = sorted(
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith(TRACKING_PREFIXES)
    )
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path,
                       urlencode(query, doseq=True), ""))


class PageCache:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.root = self.settings.cache_dir / "pages"

    def key(self, url: str) -> str:
        return hashlib.sha256(normalize_url(url).encode()).hexdigest()

    def get(self, url: str) -> FetchResult | None:
        key = self.key(url)
        conn = connect(self.settings.db_path)
        try:
            row = conn.execute("SELECT * FROM page WHERE url_hash=?", (key,)).fetchone()
        finally:
            conn.close()
        if row is None or datetime.fromisoformat(row["expires_at"]) <= datetime.now(UTC):
            return None
        body_path = Path(row["body_path"]) if row["body_path"] else None
        if body_path is None or not body_path.exists():
            return None
        html = body_path.read_text(encoding="utf-8")
        return FetchResult(url, row["url"], row["status_code"], html, "", row["tier"], True,
                           bool(row["blocked"]), row["fetched_at"])

    def put(self, result: FetchResult, *, ttl_hours: int | None = None) -> None:
        key = self.key(result.url)
        host = urlsplit(result.final_url).hostname or ""
        now = datetime.now(UTC)
        path = self.root / key[:2] / f"{key}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result.html, encoding="utf-8")
        conn = connect(self.settings.db_path)
        try:
            conn.execute(
                """INSERT INTO page(url,url_hash,host,tier,status_code,blocked,fetched_at,
                   expires_at,body_path) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(url_hash) DO UPDATE SET url=excluded.url,host=excluded.host,
                   tier=excluded.tier,status_code=excluded.status_code,blocked=excluded.blocked,
                   fetched_at=excluded.fetched_at,expires_at=excluded.expires_at,
                   body_path=excluded.body_path""",
                (
                    result.url,
                    key,
                    host,
                    result.tier,
                    result.status,
                    int(result.blocked),
                    result.fetched_at or clock.utcnow_iso(),
                    (
                        now + (
                            timedelta(hours=ttl_hours) if ttl_hours is not None
                            else timedelta(days=self.settings.cache_ttl_days)
                        )
                    ).isoformat(),
                    str(path),
                ),
            )
            conn.commit()
        finally:
            conn.close()
