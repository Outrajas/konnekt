# Decisions

## Environment and dependencies

- The repository plan names Python 3.12.10, but that interpreter was unavailable. The existing `.venv` pointed to a missing installation, so it was rebuilt with the available bundled Python 3.12.14. The project metadata accepts Python 3.12 patch releases (`>=3.12,<3.13`).
- Scrapling remains an optional `fast` extra; no browser installation is part of the baseline.
- Empty environment placeholders are ignored by settings, so copying `.env.example` leaves safe code defaults active.
- The original string enums remain `str, Enum` rather than `StrEnum`; Ruff's modernization warning is suppressed because changing `str(enum_value)` would alter existing runtime behavior.

## Platform policy

- The operator explicitly enabled logged-out LinkedIn, Indeed, and Glassdoor access with a 30–90 second spacing range, a 10-request daily cap per platform, and Tier 1 as the maximum. LinkedIn page fetches are limited to snippet misses and never bulk crawled; Indeed and Glassdoor are limited to job and hiring signals. No login, operator cookies, credentials, challenge solving, or stealth tier is allowed for these hosts. A block pauses the host for 48 hours.
