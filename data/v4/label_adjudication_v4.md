# Benchmark Label Adjudication Log (v4)

This document records the case-by-case label adjudications applied on top of the initial rule-based benchmark extraction (`ground_truth_180_verified.json`) to produce the final v4 ground truth (`ground_truth_v4.json`, 167 retained operations after applying the v4 corpus exclusion rules).

## Selection Protocol and Scope

1. **Prisma execution-oracle discrepancies (8 cases):** Every case where the initial rule-based label disagreed with the official Prisma 7.10.1 DMMF + PostgreSQL 18.7 execution oracle was inspected against the parent commit tree (`git ls-tree` / `git show <parent>:<path>`). In all eight cases, the execution oracle was confirmed correct.
2. **Django and Rails discrepant or low-confidence cases:** Cases flagged as `REVIEW` in the initial extraction or exhibiting discrepancies between initial rules and repository artefacts were inspected against the parent commit tree.
3. **Scope limitation (discrepant resolution bias):** High-confidence (`HIGH`) cases where the initial rule-based labeller and `ECV-Fast` agreed were not re-audited individually; potential shared errors are quantified separately via the blind second-rater agreement protocol (`second_rater/`).

The programmatic overrides are stored in `scripts/v4/label_overrides_v4.py`.

## Prisma: Disagreements with the Official DMMF Execution Oracle (8 cases)

| Case | Initial (v3) | Final (v4) | Primary Git / DMMF Evidence |
| --- | --- | --- | --- |
| `prisma_012` | `VIOL` | `SAFE` | `type` is not a field of `crm_Contacts` in $C_{t-1}$ `schema.prisma` (line 41 belongs to `crm_Accounts`); text hit was object key `type: "Prospect"`. |
| `prisma_020` | `VIOL` | `SAFE` | Dropped column `stakeholder_type`; $C_{t-1}$ field is `stakeholderType` without `@map`; text hit was a Zod validation field. |
| `prisma_021` | `VIOL` | `SAFE` | No `isPublic` field in any $C_{t-1}$ `.prisma` file; text hit was a Zod input schema. |
| `prisma_030` | `VIOL` | `SAFE` | `BrowserbaseContext` has no `connectionId` in the DMMF; text hit was route parameter `@Param('connectionId')`. |
| `prisma_036` | `VIOL` | `SAFE` | `soc2typeii*` absent from $C_{t-1}$ `.prisma` files and non-migration commit diff. |
| `prisma_052` | `VIOL` | `SAFE` | `User` model of the `todoApp` example has no `email` column (3 scalars in DMMF); text hit was in a different example application. |
| `prisma_039` | `SAFE` | `VIOL` | Column added and set `NOT NULL` without a database default in the same migration; oracle raises `SQLSTATE 23502` on $C_{t-1}$ insert. |
| `prisma_057` | `SAFE` | `VIOL` | Column added and set `NOT NULL` without a database default in the same migration; oracle raises `SQLSTATE 23502` on $C_{t-1}$ insert. |
| `prisma_002` | `SAFE` | `VIOL` | Multi-file Prisma schema (`packages/database/prisma/models/event.prisma`) defines `ScheduledEvent` (`@@map("scheduled_events")`) in $C_{t-1}$; migration backfills existing prod violations and adds a validated `CHECK` constraint. |
| `prisma_038` | `SAFE` | `VIOL` | `packages/database/prisma/models/space.prisma:20` defines `enum SpaceMemberRole` (including `OWNER`) in $C_{t-1}$; migration rebuilds the enum removing `OWNER` in place (`SpaceMemberRole_new -> SpaceMemberRole`). |

## Django Adjudications

| Case | Initial (v3) | Final (v4) | Primary Git Evidence |
| --- | --- | --- | --- |
| `django_138` | `VIOL` | `SAFE` | $C_{t-1}$ `Member` model defines `role`, not `rw`; `rw` appears only as a form field (staged contract). |
| `django_142` | `VIOL` | `SAFE` | `SubEvent.variations` is not a $C_{t-1}$ model field; text hit was `active_variations` queryset annotation. |
| `django_146` | `VIOL` | `SAFE` (excluded) | `IngredientWeightUnit` has no `unit` field; text hit was in a docstring; commit also rewrites migration files (excluded by v4 rules). |
| `django_147` | `VIOL` | `SAFE` | No `ContestSubmission` model exists in $C_{t-1}$; text hits were `ContestSubmissionAPIView` and tests. |
| `django_124` | `VIOL` | `SAFE` | `DefaultGuest` is a proxy model (`'proxy': True` in `0003`); `DeleteModel` emits no database DDL. |
| `django_139` | `VIOL` | `SAFE` | `CheckSessions` is a proxy model in the same app; `DeleteModel` emits no database DDL. |
| `django_125` | `SAFE` | `SAFE` | Rationale updated: app and `0001_initial` added in the same commit (12 initial migrations). |
| `django_148` | `SAFE` | `SAFE` | Rationale updated: entire migration history including `0001_initial` added in the same commit. |
| `django_152` | `SAFE` | `VIOL` | `auth_user` is Django's built-in `django.contrib.auth.models.User` table active in $C_{t-1}$ with non-unique `email`; `RunSQL` adds a validated `UNIQUE (email)` constraint. |
| `django_165` | `SAFE` | `SAFE` | Rationale sharpened: $C_{t-1}$ `Domain.validation_process_start` (`readthedocs/projects/models.py:1836`) uses `auto_now_add=True` with `# TODO: remove after deploy.` and `django-safemigrate` (`Safe.always`), confirming a staged contract. |
| `django_134` | `VIOL` | excluded | Commit `"perf: squash migrations"` deletes `0005`–`0009` and adds a merged file (`rewritten_history`). |
| `django_126`, `django_131` | — | excluded | Operations belong to squashed migration files declaring `replaces` (`squashed_migration`). |

## Rails Adjudications

| Case | Initial (v3) | Final (v4) | Primary Git Evidence |
| --- | --- | --- | --- |
| `rails_066` | `SAFE` (`REVIEW`) | `VIOL` | `spree/core/app/models/spree/stock_item.rb` (`Spree::StockItem`) maps to `spree_stock_items` in $C_{t-1}$. |
| `rails_078` | `SAFE` (`REVIEW`) | `VIOL` | `app/models/federails/quote_authorization.rb` exists and maps to the renamed table in $C_{t-1}$. |
| `rails_084` | `SAFE` (`REVIEW`) | `VIOL` | The `federails` engine plus application overrides under `app/models/federails/` use `federails_hosts` in $C_{t-1}$. |
| `rails_081` | `SAFE` (`REVIEW`) | `VIOL` | `Decidim::Comments::Comment < ApplicationRecord` loads all columns of `decidim_comments_comments` in $C_{t-1}$; column is not ignored. |
| `rails_105` | `SAFE` (`REVIEW`) | `SAFE` | Renames partitions of a partitioned search table; migration parser read Ruby variable `old` as table name. |
| `rails_093` | `VIOL` (`REVIEW`) | `SAFE` | `rapns_notifications` is created earlier inside the same `rpush` installation migration. |
| `rails_087` | `VIOL` | `SAFE` | $C_{t-1}$ `app/models/user.rb:52` declares `self.ignored_columns += ["creation_ip"]`. |
| `rails_088` | `VIOL` | `SAFE` | Unused `sessions` table dropped with `if_exists: true`; no ActiveRecord model; text hit was `poll_sessions`. |

## Unchanged Edge Cases Noted During Audit

- `rails_089` (Alchemy `roles` column rename): retained as `VIOL`. The migration file was newly added in the commit (extracted from another migration) while $C_{t-1}$ controller code references `roles`.
- `rails_100` (`gitlabhq/gitlabhq@30ec324f`): falls outside the 150-commit sliding window in the v4 re-mine of `gitlabhq/gitlabhq`; its v4 `ECV-Fast` verdict (`UNCERTAIN`, table `p_duo_workflows_checkpoint_headers` absent from $O_{t-1}$) is verified directly and matches the retained verdict.
