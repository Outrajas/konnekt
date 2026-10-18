# Live run sequence

Run these commands from the repository root in Windows PowerShell. They use the
operator environment at `\.venv`; the agent environment is `\.venv-agent`.
Live network activity is performed by the operator through Leadforge's router
and the caps in `data/domain_policy.toml`.

```powershell
.\.venv\Scripts\leadforge.exe seed-companies --dry-run
.\.venv\Scripts\leadforge.exe seed-companies
.\.venv\Scripts\leadforge.exe run-daily --dry-run
.\.venv\Scripts\leadforge.exe run-daily
.\.venv\Scripts\leadforge.exe report
.\.venv\Scripts\leadforge.exe export-run-summary
.\.venv\Scripts\leadforge.exe drafts
.\.venv\Scripts\leadforge.exe today
```

The first command prints the planned sources and confirms that no request will
be made. Review the source order and expected policy budgets before running the
live seed command. Seeding accepts only unique companies with an official
registrable domain, a matching open early-career role or documented invite-email
signal, a resolving DNS name, and a homepage that names the company. At least
8 companies must pass; if fewer pass, seeding stops and leaves the current
companies file untouched. If it finds 8–14, the pipeline may proceed with those
results. The target is 25.

`run-daily --dry-run` previews the capped discover, people, enrich, and report
stages. `run-daily` executes them with a lock and resumable stage state. Review
`report` for detailed local pipeline metrics; `export-run-summary` writes a
counts-only JSON summary to `data/output/run_summary.json`. `drafts` creates
manual-review drafts and `today` lists eligible manual outreach under the
current daily ramp cap. No command sends messages.

## Stops and source blocks

- **Fewer than 8 accepted companies:** seed stops and reports per-source page,
  company, block, and robots-refusal counts. Check the source results and
  acceptance evidence before continuing.
- **8–14 accepted companies:** this is the documented fallback range; seeding
  proceeds with the accepted set.
- **Robots refusal:** that host is unavailable for the current run. Leadforge
  skips it and continues with later sources.
- **Block, challenge, HTTP 401/403, or HTTP 429:** the router pauses that host
  for 48 hours. The run skips that host and continues with other sources. Do
  not retry the host early or escalate its access tier.
- **Budget exhausted or policy-disabled host:** Leadforge skips that source for
  the run; the configured policy cap remains in force.
- **Run-daily lock already exists:** another run may be active. Do not remove
  the lock while that run is active; after confirming it ended, retry the
  command.
- **Run-daily stage error:** completed stages are saved for resumption. Inspect
  the reported stage and local `report` output before restarting.

If the companies file is missing or empty, `run-daily --seed` can seed first;
the sequence above runs the explicit seed command so its plan and result are
visible before the pipeline starts.
