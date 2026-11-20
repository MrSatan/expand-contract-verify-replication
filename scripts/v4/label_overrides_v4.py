"""v4 label adjudications for the 180-case benchmark (applied on top of research/v3/ground_truth_180_verified.json).

Every entry was checked against the parent-commit tree of the local clone (C_{t-1} ORM schema/models,
the exact referenced source line, and the commit's file list). Evidence is summarised in
label_adjudication_v4.md.
"""

OVERRIDES: dict[str, tuple[bool, str]] = {
    # --- Prisma: the 8 cases where the DMMF execution oracle disagreed with the v3 label ---
    "prisma_012": (False, "SAFE (v4): `type` is not a field of crm_Contacts in the C_{t-1} schema.prisma; the grep hit is an object literal key in a route."),
    "prisma_020": (False, "SAFE (v4): dropped column `stakeholder_type` is not in C_{t-1} Prisma (field is camelCase `stakeholderType`, no @map); grep hit is a zod field."),
    "prisma_021": (False, "SAFE (v4): `isPublic` is not a field of Update in C_{t-1} schema.prisma; grep hit is a zod input schema."),
    "prisma_030": (False, "SAFE (v4): BrowserbaseContext has no `connectionId` in C_{t-1}; grep hit is an unrelated route parameter."),
    "prisma_036": (False, "SAFE (v4): `soc2typeii_status` absent from C_{t-1} Prisma schema and from the non-migration diff."),
    "prisma_052": (False, "SAFE (v4): C_{t-1} User model of this example app has no `email`; grep hit is in a different example app."),
    "prisma_039": (True, "VIOL (v4): column added and set NOT NULL without default in the same migration; C_{t-1} inserts fail (oracle: 23502)."),
    "prisma_057": (True, "VIOL (v4): column added and set NOT NULL without default in the same migration; C_{t-1} inserts fail (oracle: 23502)."),
    # --- Prisma: multi-file schema & enum-rebuild corrections verified against C_{t-1} ---
    "prisma_002": (True, "VIOL (v4): ScheduledEvent (@@map(\"scheduled_events\")) is active in C_{t-1} (packages/database/prisma/models/event.prisma); migration adds validated CHECK constraint after backfilling prod violations."),
    "prisma_038": (True, "VIOL (v4): SpaceMemberRole enum is active in C_{t-1} (packages/database/prisma/models/space.prisma:20); migration rebuilds enum removing value OWNER in place."),
    # --- Django: grep-based VIOL labels not supported by C_{t-1} code, plus built-in auth_user & staged contract ---
    "django_138": (False, "SAFE (v4): Member.rw was already removed from the C_{t-1} model (replaced by `role`); grep hit is a form field."),
    "django_142": (False, "SAFE (v4): SubEvent.variations is not a field of the C_{t-1} model; grep hit is an annotation name."),
    "django_146": (False, "SAFE (v4): IngredientWeightUnit has no `unit` field in C_{t-1}; grep hit is a docstring."),
    "django_147": (False, "SAFE (v4): no ContestSubmission model in C_{t-1}; grep hits are API view/test classes."),
    "django_124": (False, "SAFE (v4): DefaultGuest is a proxy model (proxy=True); DeleteModel emits no DDL."),
    "django_139": (False, "SAFE (v4): CheckSessions is a proxy model (proxy=True); DeleteModel emits no DDL."),
    "django_125": (False, "SAFE (v4): the app and its 0001_initial migration are added in the same commit; the table does not exist under C_{t-1}."),
    "django_148": (False, "SAFE (v4): the app's migration history including 0001_initial is added in the same commit; no C_{t-1} table."),
    "django_152": (True, "VIOL (v4): auth_user is Django's built-in User table active in C_{t-1} with non-unique email; RunSQL adds validated UNIQUE(email) constraint."),
    "django_165": (False, "SAFE (v4): C_{t-1} Domain.validation_process_start (readthedocs/projects/models.py:1836) sets auto_now_add=True with '# TODO: remove after deploy' and django-safemigrate Safe.always (staged contract)."),
    # --- Rails: REVIEW cases resolved from the C_{t-1} tree ---
    "rails_066": (True, "VIOL (v4): Spree::StockItem (spree/core/app/models/spree/stock_item.rb) uses spree_stock_items in C_{t-1}."),
    "rails_078": (True, "VIOL (v4): app/models/federails/quote_authorization.rb in C_{t-1} maps to the renamed table."),
    "rails_084": (True, "VIOL (v4): the federails engine loaded by C_{t-1} (with app overrides under app/models/federails) uses federails_hosts."),
    "rails_081": (True, "VIOL (v4): Decidim::Comments::Comment (ApplicationRecord) loads all columns of decidim_comments_comments; column not ignored."),
    "rails_105": (False, "SAFE (v4): renames partitions of a partitioned search table; C_{t-1} queries the parent table. Parser extracted the variable `old` as table name."),
    "rails_093": (False, "SAFE (v4): rapns_notifications is created earlier in the same migration file (rpush install migration)."),
    "rails_087": (False, "SAFE (v4): C_{t-1} User model has `self.ignored_columns += [\"creation_ip\"]` (staged contract)."),
    "rails_088": (False, "SAFE (v4): unused `sessions` table dropped with if_exists; no model in C_{t-1}; grep hit was `poll_sessions`."),
}
