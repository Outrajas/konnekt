# Decisions

## Environment and dependencies

- The repository plan names Python 3.12.10, but that interpreter was unavailable. The existing `.venv` pointed to a missing installation, so it was rebuilt with the available bundled Python 3.12.14. The project metadata accepts Python 3.12 patch releases (`>=3.12,<3.13`).
- Scrapling remains an optional `fast` extra; no browser installation is part of the baseline.
