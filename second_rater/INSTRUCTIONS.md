# Second-rater instructions (60 cases)

For each row of `second_rater_sheet_60.csv`, decide whether the **previous release** of the application (the code at the parent commit, `C_{t-1}`) would fail at runtime if the migration operation in the row ran **before** the new code was rolled out.

- `VIOL`: `C_{t-1}` would read or write the dropped, renamed, retyped, or newly required symbol (including implicit ORM projection: Prisma selects all fields declared in `schema.prisma`; Rails loads every column not listed in `ignored_columns`; Django selects every model field) or would insert rows that the new constraint rejects.
- `SAFE`: `C_{t-1}` no longer uses the symbol (e.g., removed from the ORM model in an earlier release, `ignored_columns`, `SeparateDatabaseAndState`), the table/column was created in the same migration, or the change is value-compatible (e.g., `varchar` to `text`).
- `UNSURE`: you cannot decide from the commit and the parent tree within about 10 minutes.

Use `commit_url` to see the migration and the diff, and `parent_tree_url` to browse `C_{t-1}`. Do not look at `benchmark_audit_180.md` or any tool output while labelling. Optionally fill in `rater_confidence_1to3` and a short `rater_note`.

When finished, run:

```bash
python kappa.py second_rater_sheet_60.csv
```
