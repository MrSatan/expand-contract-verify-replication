# Replication package

**Paper:** *Does the Previous Release Still Work? Cross-Commit Verification of Dual-Version Compatibility in ORM Schema Migrations*
**Author:** Arvin Rezaei (University of Kurdistan)

This package contains the tool, the data, and the scripts behind every number, table, and figure in the paper. Tool versions: Python 3.13, Node.js 24, PGlite 0.5.17 (PostgreSQL 18.7), `@prisma/internals` 7.10.1, `squawk` 2.73.0, Ruby 4.0 with ActiveRecord 8.1 and `strong_migrations` 2.8.0, `django-migration-linter` 7.0.0 (PostgreSQL analyser).

**Current version: v4 (submission freeze).** The paper reports the v4 results in `data/v4/`. Earlier v2/v3 files are kept for traceability; where they differ, v4 is authoritative. No further pipeline changes or re-mining (no v5) are planned prior to peer review.

## Layout

| Path | Content |
| --- | --- |
| `ecv/` | ExpandContractVerify and ExpandContractSynth: DDL parsers (`ddl_parser.py`), ORM parsers (`orm_parser.py`), verifier (`verifier.py`), AST scanner for ECV-Deep (`deep_scanner.py`), plan generator (`synthesizer.py`), unit tests (`test_ecv.py`). |
| `scripts/phase1/` | Repository discovery (`discover_repos.py`) and corpus mining (`mine_corpus.py`). |
| `scripts/v2/` | Blind benchmark sampling, ground-truth labelling, real baseline runs, Prisma DMMF oracle, minimal-schema plan replay. |
| `scripts/v3/` | Final RQ1 recompute, ECV verdicts on the benchmark, final RQ3 metrics, `strong_migrations` with real column metadata, full-schema replay with the real ECS plans, RQ2 issue retrieval, second-rater sheet, figures. |
| `data/corpus_repos.json` | The 110 repositories with selection metadata and verified Kubernetes/Helm flags. |
| `data/rq1/` | Mined verdicts per repository (`mined_repos/`) and the RQ1 summary (`rq1_added_only_summary.json`). |
| `data/benchmark/` | 180 sampled operations with evidence (`benchmark_v2_raw.json`), strata sizes, initial and final labels (`ground_truth_180_verified.json`), baseline and ECV outputs, final metrics (`rq3_v3_final_metrics.json`). |
| `benchmark_audit_180.md` | (v3, superseded for labels) Human-readable table of the 180 sampled cases with commit links; final labels and rationales are in `data/v4/ground_truth_v4.json` and `data/v4/label_adjudication_v4.md`. |
| `data/rq2/` | Full text of the 14 candidate issue threads (`rq2_issue_texts.json`) and the inclusion decisions (`rq2_v3_verified.json`). |
| `data/rq4/` | Plan replay results: minimal schemas (`synth_v2_out.json`, `rq4_and_rq2_squawk_v2.json`) and full Prisma histories (`full_schema_v3_results.json`, `rq4_full_schema_v3_summary.json`). |
| `scripts/v4/` | v4 pipeline used in the paper: RQ1 with exclusion rules and cluster bootstrap (`rq1_v4_recompute.py`), backward-compatibility statistics and full-history validity filter (`rq1_v4_backward.py`), benchmark evaluation with label adjudications, post-stratified IPW, McNemar tests and minimal-schema replay (`rq3_v4_eval.py`, `label_overrides_v4.py`), figures (`make_v4_figures.py`). |
| `data/v4/` | v4 results: `rq1_v4_summary.json`, `rq1_v4_backward.json` (Tables 2--4, RQ3 full histories), `rq3_v4_metrics.json` (Tables 5 and 7), `ground_truth_v4.json` (167 final labels), `synth_v4_out.json`, `added_only_pool_v4.json` (population frame), `label_adjudication_v4.md` (evidence for every adjudicated label), progress logs. |
| `second_rater/` | Blind 60-case labelling sheet and `kappa.py` (agreement, Cohen's kappa with bootstrap CI, PABAK against `ground_truth_v4.json`). |

## Mapping from paper to artefacts

| Paper item | Produced by | Data |
| --- | --- | --- |
| **v4 (current):** Tables 2--4, Figures 2--3 (RQ1) | `scripts/phase1/mine_corpus.py` with `ECV_MINED_DIR`/`ECV_TAG=v4`, `scripts/v4/rq1_v4_recompute.py`, `scripts/v4/rq1_v4_backward.py`, `scripts/v4/make_v4_figures.py` | `data/rq1/mined_repos/`, `data/v4/` |
| **v4 (current):** Tables 5 and 7, Figure 4, IPW, McNemar (RQ2--RQ3) | `scripts/v4/rq3_v4_eval.py` (uses the v2/v3 baseline outputs in `data/benchmark/`) | `data/v4/rq3_v4_metrics.json` |
| Table 1 (practitioner threads) | `scripts/v3/fetch_rq2_issues.py` + manual reading | `data/rq2/` |
| (v3, superseded) RQ1 | `scripts/phase1/mine_corpus.py` (with `ECV_MINED_DIR`), `scripts/v3/rq1_v3_recompute.py`, `scripts/v3/make_v3_figures.py` | `data/rq1/` |
| (v3, superseded) benchmark metrics | `scripts/v2/sample_and_extract_benchmark.py`, `scripts/v2/audit_ground_truth_180.py`, `scripts/v2/run_real_baselines_and_oracles.py`, `scripts/v3/build_sm_columns_input.py`, `scripts/v3/run_real_strong_migrations_v3.rb`, `scripts/v3/ecv_eval_v3.py`, `scripts/v3/finalize_rq3_v3.py` | `data/benchmark/` |
| Table 6 (runtime) | `scripts/v2/run_real_baselines_and_oracles.py` | `data/benchmark/baselines_and_oracles_v3.json` (`runtime_summary`) |
| Plan replay runs (inputs to v4 Table 7) | `scripts/v2/run_v2_synth_and_full_schema.py` (minimal), `scripts/v3/run_v3_full_schema.py` + `pg_v3_full_schema_worker.mjs` (full histories) | `data/rq4/` |

## Running

```bash
python -m pip install -r requirements.txt
npm install
python ecv/test_ecv.py
python reproduce_tables.py
```

`reproduce_tables.py` prints every table (Tables 2--7) directly from `data/v4/` in a few seconds. The full re-mining and replay scripts support setting `ECV_TASK_DIR` to the package or workspace root, `ECV_TOOLS_DIR` to the directory containing `node_modules/`, and `SQUAWK_BIN` to the `squawk` binary (`SQUAWK_USE_WSL=1` on Windows if using a Linux binary via WSL). Re-mining clones the 110 repositories as treeless partial clones (several GB) and takes about 5--10 minutes on 8 cores; every long-running script prints progress lines with percentage, counts, elapsed time, and ETA.

## Notes and limitations

- Final labels (`ground_truth_v4.json`, 167 cases: 106 `VIOL`, 61 `SAFE`) come from an initial rule-based procedure over repository artefacts plus 46 author-verified case-by-case adjudications (`VERIFIED_MANUAL`, `ADJUDICATED_V4`); evidence is in `data/v4/label_adjudication_v4.md` (English). Thirteen of the 180 sampled operations lie in files excluded by the v4 rules (squashed, rewritten history, re-added). High-confidence (`HIGH`) agreements (121 cases) were not re-audited; `rq3_v4_metrics.json` reports both initial rule-based (`pre_initial_rule_based`) and intermediate pre-v4 (`pre_v4_adjudication`) metrics alongside final metrics. A blind 60-case audit sheet and `kappa.py` are provided in `second_rater/` for independent verification.
- Baseline SQL translation: `django-migration-linter`'s `PostgresqlAnalyser` (and `squawk` on Rails/Django pooled rows) run on PostgreSQL SQL translated from the parsed migration operation rather than live `manage.py sqlmigrate`; in `scripts/v4/rq3_v4_eval.py`, the three verified DDL-free Django no-ops (`django_124`, `django_139` proxy-model `DeleteModel`, and `django_153` same-PG-type field subclass rename) receive empty SQL so SQL-based linters do not incur synthetic false positives.
- v4 ECV verdicts: tables missing from the parent ORM snapshot are `UNCERTAIN` across all table/column operators (including `ADD_NOT_NULL_NO_DEFAULT`), unless a table drop or rename is explicitly staged; Rails tables without an ActiveRecord class are `UNCERTAIN`; `RENAME_TYPE_ENUM` checks `orm_old.enums`; squashed Django migrations are skipped.
- `rails_100` (`gitlabhq/gitlabhq@30ec324f`) lies outside the 150-commit sliding window of the v4 `gitlabhq` re-mine (`unmatched_kept_previous_verdict` in `rq3_v4_metrics.json`); its v4 `ECV-Fast` verdict (`UNCERTAIN`) was verified directly and matches the retained verdict.
- `ExpandContractSynth` emits executable SQL plans for 7 operators (89/94 verified in minimal schemas) and advisory-only commented templates (`executable_sql: false`) for `ADD_UNIQUE_OR_CHECK` (9) and `RENAME_TYPE_ENUM` (3), which are excluded from the execution pass rate.
- `benchmark_v2_raw.json` contains excerpts of source files from the studied open-source repositories, kept only so that the labels can be checked; they remain under their original licences.
- `strong_migrations` runs with a stub PostgreSQL adapter; column metadata is not available for every case (`rails_60_in_v3.json`); 50 of the 58 v4 Rails cases executed.
- All `scripts/v4/` scripts support setting `ECV_TASK_DIR` to the replication package root (with `scripts/progress.py` bundled in the package).

## Licence

Code: MIT. Repository lists, labels, and aggregate data: CC BY 4.0.
