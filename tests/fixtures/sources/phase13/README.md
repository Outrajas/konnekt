# Phase 13 live response snapshots

Captured 2026-10-15 by one request per endpoint through `Fetcher` and the
configured domain policy. The fixture JSON keeps the observed response shape
and a small subset of its records. Ashby description text was omitted because it
contained incidental contact data; all fixtures contain no email addresses.

- Greenhouse: `https://boards-api.greenhouse.io/v1/boards/stripe/jobs`
- Lever: `https://api.lever.co/v0/postings/xmlexample?mode=json` (vendor example board)
- Ashby: `https://api.ashbyhq.com/posting-api/job-board/linear`
- Hacker News Firebase: `https://hacker-news.firebaseio.com/v0/topstories.json` (first 20 IDs)

The initial guessed Greenhouse and Lever board identifiers returned 404; those
responses were not used as fixtures. The accepted endpoints above returned 200.
