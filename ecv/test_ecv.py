"""Unit tests for ExpandContractVerify (parsers, verifier) and ExpandContractSynth."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ecv.ddl_parser import parse_migration
from ecv.orm_parser import parse_prisma_schema, parse_rails_schema_and_models, parse_django_models
from ecv.verifier import verify_op, strong_migrations_baseline_verdict
from ecv.synthesizer import synthesize_expand_contract


def run_tests():
    # 1. Prisma: DROP_COLUMN (unsafe) + SET_NOT_NULL (unsafe) + RENAME_TYPE_ENUM (unsafe) + Safe Contract DROP
    sql = '''
    ALTER TABLE "User" DROP COLUMN "legacyFlag", ALTER COLUMN "email" SET NOT NULL;
    ALTER TABLE "User" DROP COLUMN "alreadyRemoved";
    ALTER TYPE "Role_new" RENAME TO "Role";
    '''
    ops_p = parse_migration("prisma", sql, "prisma/migrations/20260101/migration.sql")
    assert len(ops_p) == 4, f"Expected 4 ops, got {len(ops_p)}: {ops_p}"

    orm_p_old = parse_prisma_schema({
        "schema.prisma": """
        enum Role {
          ADMIN
          USER
        }
        model User {
          id Int @id
          legacyFlag Boolean?
          email String?
        }
        """
    })
    orm_p_new = parse_prisma_schema({
        "schema.prisma": """
        enum Role {
          ADMIN
          USER
        }
        model User {
          id Int @id
          email String
        }
        """
    })
    verdicts_p = [verify_op(o, orm_p_old, orm_p_new) for o in ops_p]
    assert verdicts_p[0].classification == "DUAL_VERSION_VIOLATION_BACKWARD", verdicts_p[0]
    assert verdicts_p[1].classification == "DUAL_VERSION_VIOLATION_BACKWARD", verdicts_p[1]
    assert verdicts_p[2].classification == "SAFE_CONTRACT", verdicts_p[2]
    assert verdicts_p[3].classification == "DUAL_VERSION_VIOLATION_BOTH", verdicts_p[3]
    print("PASS Test 1 (Prisma):", [(v.op.kind, v.op.column, v.classification) for v in verdicts_p])

    # 2. Rails: safety_assured unsafe drop vs ignored_columns safe Contract vs down() false alarm
    rb = """
    class TestMig < ActiveRecord::Migration[7.1]
      def up
        safety_assured { remove_column :users, :legacy_col, :string }
        remove_column :users, :ignored_col, :string
      end
      def down
        drop_table :new_table
      end
    end
    """
    ops_r = parse_migration("rails", rb, "db/migrate/2026_test.rb")
    assert len(ops_r) == 3, f"Expected 3 rails ops, got {ops_r}"
    orm_r = parse_rails_schema_and_models(
        'create_table "users" do |t|\n  t.string "legacy_col"\n  t.string "ignored_col"\nend',
        {"app/models/user.rb": 'class User < ApplicationRecord\n  self.ignored_columns += ["ignored_col"]\nend'},
    )
    verdicts_r = [verify_op(o, orm_r, orm_r) for o in ops_r]
    assert verdicts_r[0].classification == "DUAL_VERSION_VIOLATION_BACKWARD", verdicts_r[0]
    assert strong_migrations_baseline_verdict(ops_r[0])["bypassed"] is True  # FN for strong_migrations!
    assert verdicts_r[1].classification == "SAFE_CONTRACT", verdicts_r[1]
    assert strong_migrations_baseline_verdict(ops_r[1])["flagged"] is True   # FP for strong_migrations!
    assert verdicts_r[2].classification == "ROLLBACK_ONLY_FALSE_ALARM", verdicts_r[2]
    print("PASS Test 2 (Rails):", [(v.op.kind, v.op.column, v.classification) for v in verdicts_r])

    # 3. Django: RemoveField + AddField(default without db_default) + RunSQL(reverse_sql)
    dj = '''
from django.db import migrations, models
class Migration(migrations.Migration):
    operations = [
        migrations.RemoveField(model_name="monitor", name="is_upserting"),
        migrations.AddField(model_name="monitor", name="new_req", field=models.CharField(default="x", max_length=10)),
        migrations.RunSQL(sql="SELECT 1;", reverse_sql="ALTER TABLE sentry_monitor DROP COLUMN foo;"),
    ]
'''
    ops_d = parse_migration("django", dj, "sentry/migrations/0042_test.py")
    assert len(ops_d) == 3, f"Expected 3 django ops, got {ops_d}"
    orm_d = parse_django_models({
        "sentry/models.py": 'class Monitor(models.Model):\n    is_upserting = models.BooleanField(default=False)\n'
    })
    verdicts_d = [verify_op(o, orm_d, orm_d) for o in ops_d]
    assert verdicts_d[0].classification == "DUAL_VERSION_VIOLATION_BACKWARD", verdicts_d[0]
    assert verdicts_d[1].classification == "DUAL_VERSION_VIOLATION_BACKWARD", verdicts_d[1]
    assert verdicts_d[2].classification == "ROLLBACK_ONLY_FALSE_ALARM", verdicts_d[2]
    print("PASS Test 3 (Django):", [(v.op.kind, v.op.column, v.classification) for v in verdicts_d])

    # 4. Synthesizer
    for o in [ops_p[0], ops_p[1], ops_d[1]]:
        plan = synthesize_expand_contract(o)
        assert plan.supported and plan.phase1_expand_sql and plan.phase3_contract_sql
    print("PASS Test 4 (Synthesizer): all plans valid")

    # 5. v3: GitLab `ignore_column` helper is recognised as a C_{t-1} detach
    schema_rb = '''
    create_table "issues", force: :cascade do |t|
      t.string "title"
      t.integer "legacy_weight"
    end
    '''
    model_ign = {"app/models/issue.rb": "class Issue < ApplicationRecord\n  ignore_column :legacy_weight, remove_with: '17.0', remove_after: '2024-05-01'\nend\n"}
    orm_ign = parse_rails_schema_and_models(schema_rb, model_ign)
    assert "legacy_weight" in orm_ign.models_by_table["issues"].ignored_columns
    model_ign2 = {"app/models/issue.rb": "class Issue < ApplicationRecord\n  ignore_columns %i[a_col b_col], remove_with: '17.1'\nend\n"}
    assert {"a_col", "b_col"} <= parse_rails_schema_and_models(schema_rb, model_ign2).models_by_table["issues"].ignored_columns
    op_rm = parse_migration("rails", "class X < Gitlab::Database::Migration[2.2]\n  def up\n    remove_column :issues, :legacy_weight\n  end\nend\n", "db/migrate/20260101_x.rb")[0]
    v5 = verify_op(op_rm, orm_ign, orm_ign)
    assert v5.classification == "SAFE_CONTRACT", v5
    print("PASS Test 5 (ignore_column helper):", v5.classification)

    # 6. v3: Rails post-deploy drop is judged against C_t (ignored in C_t -> safe; still loaded -> violation)
    mig_pd = "class Y < ActiveRecord::Migration[7.1]\n  def up\n    remove_column :issues, :legacy_weight\n  end\nend\n"
    op_pd = parse_migration("rails", mig_pd, "db/post_migrate/20260102_y.rb")[0]
    assert op_pd.extra.get("post_deploy") is True
    orm_plain = parse_rails_schema_and_models(schema_rb, {"app/models/issue.rb": "class Issue < ApplicationRecord\nend\n"})
    assert verify_op(op_pd, orm_plain, orm_ign).classification == "SAFE_CONTRACT"
    assert verify_op(op_pd, orm_plain, orm_plain).classification == "DUAL_VERSION_VIOLATION_BACKWARD"
    print("PASS Test 6 (Rails post_migrate judged against C_t)")

    # 7. v3: Sentry SafeRemoveField (DELETE emits DDL, MOVE_TO_PENDING is state-only) + is_post_deployment
    dj = '''
from sentry.new_migrations.migrations import CheckedMigration
from sentry.new_migrations.monkey.fields import SafeRemoveField
from sentry.new_migrations.monkey.state import DeletionAction
class Migration(CheckedMigration):
    is_post_deployment = True
    operations = [
        SafeRemoveField(model_name="monitor", name="is_upserting", deletion_action=DeletionAction.DELETE),
        SafeRemoveField(model_name="monitor", name="other_flag", deletion_action=DeletionAction.MOVE_TO_PENDING),
    ]
'''
    ops7 = parse_migration("django", dj, "src/sentry/monitors/migrations/0099_x.py")
    assert len(ops7) == 1 and ops7[0].kind == "DROP_COLUMN" and ops7[0].extra.get("post_deploy"), ops7
    print("PASS Test 7 (Sentry SafeRemoveField + is_post_deployment)")

    # 8. v3: CHECK/UNIQUE on an active table is DATA_DEPENDENT, not a confirmed violation
    op_ck = parse_migration("prisma", 'ALTER TABLE "User" ADD CONSTRAINT "u_chk" CHECK ("email" <> \'\');', "prisma/migrations/2/migration.sql")
    ck = [o for o in op_ck if o.kind == "ADD_UNIQUE_OR_CHECK"]
    if ck:
        assert verify_op(ck[0], orm_p_old, orm_p_old).classification == "DATA_DEPENDENT"
        print("PASS Test 8 (constraint -> DATA_DEPENDENT)")

    # 9. v3: synthesized SQL keeps Prisma's quoted mixed-case identifiers
    op9 = [o for o in parse_migration("prisma", 'ALTER TABLE "Chat" DROP COLUMN "threadId";', "prisma/migrations/3/migration.sql") if o.is_destructive][0]
    p9 = synthesize_expand_contract(op9)
    assert p9.phase3_contract_sql[-1] == 'ALTER TABLE "Chat" DROP COLUMN IF EXISTS "threadId";', p9.phase3_contract_sql
    print("PASS Test 9 (identifier case preserved in synthesized SQL)")

    # 10. v4: Django squashed migration (`replaces`) is not executed on an existing database
    dj_sq = '''
from django.db import migrations
class Migration(migrations.Migration):
    replaces = [("shop", "0001_initial"), ("shop", "0002_auto")]
    operations = [migrations.RemoveField(model_name="item", name="legacy")]
'''
    ops10 = parse_migration("django", dj_sq, "shop/migrations/0001_squashed_0002_auto.py")
    orm_dj = parse_django_models({"shop/models.py": "from django.db import models\nclass Item(models.Model):\n    legacy = models.TextField()\n"})
    assert ops10 and ops10[0].extra.get("squashed"), ops10
    assert verify_op(ops10[0], orm_dj, orm_dj).classification == "SQUASHED_REPLAY"
    print("PASS Test 10 (squashed migration -> SQUASHED_REPLAY)")

    # 11. v4: a table missing from O_{t-1} is UNCERTAIN unless the drop is explicitly staged
    dj_drop = '''
from django.db import migrations
class Migration(migrations.Migration):
    operations = [migrations.DeleteModel(name="Ghost")]
'''
    dj_staged = '''
from django.db import migrations
class Migration(migrations.Migration):
    operations = [migrations.SeparateDatabaseAndState(database_operations=[migrations.DeleteModel(name="Ghost")])]
'''
    op11 = parse_migration("django", dj_drop, "shop/migrations/0003_x.py")[0]
    op11s = parse_migration("django", dj_staged, "shop/migrations/0004_x.py")[0]
    assert verify_op(op11, orm_dj, orm_dj).classification == "UNCERTAIN"
    assert op11s.extra.get("staged") and verify_op(op11s, orm_dj, orm_dj).classification == "SAFE_CONTRACT"
    print("PASS Test 11 (unresolved table -> UNCERTAIN; staged drop -> SAFE_CONTRACT)")

    # 12. v4: Rails table in schema.rb without an ActiveRecord class -> UNCERTAIN
    schema_rb = 'ActiveRecord::Schema[7.1].define(version: 1) do\n  create_table "taggings" do |t|\n    t.string "context"\n  end\nend\n'
    orm_r = parse_rails_schema_and_models(schema_rb, {})
    op12 = [o for o in parse_migration("rails", "class X < ActiveRecord::Migration[7.1]\n  def change\n    remove_column :taggings, :context\n  end\nend\n", "db/migrate/2_x.rb") if o.is_destructive][0]
    assert verify_op(op12, orm_r, orm_r).classification == "UNCERTAIN"
    print("PASS Test 12 (Rails table without model class -> UNCERTAIN)")

    # 13. v4.1: ADD_NOT_NULL_NO_DEFAULT on unresolved table -> UNCERTAIN; advisory-only ECS plans flagged; datetime type fixed
    dj_unres = '''
from django.db import migrations, models
class Migration(migrations.Migration):
    operations = [migrations.AddField(model_name="historicalitem", name="flag", field=models.BooleanField(default=False))]
'''
    op13 = parse_migration("django", dj_unres, "shop/migrations/0005_x.py")[0]
    assert verify_op(op13, orm_dj, orm_dj).classification == "UNCERTAIN"
    from ecv.synthesizer import _infer_pg_type
    assert _infer_pg_type("datetime") == "TIMESTAMP(3)"
    assert _infer_pg_type("date") == "DATE"
    assert synthesize_expand_contract(ops_p[3]).executable_sql is False
    if ck:
        assert synthesize_expand_contract(ck[0]).executable_sql is False
    op_rt = parse_migration("prisma", 'ALTER TABLE "OldT" RENAME TO "NewT";', "prisma/migrations/4/migration.sql")[0]
    p_rt = synthesize_expand_contract(op_rt)
    assert p_rt.executable_sql and p_rt.phase3_contract_sql[0].startswith("BEGIN;")
    print("PASS Test 13 (unresolved ADD_NOT_NULL -> UNCERTAIN; advisory_only plans; datetime type; atomic RENAME_TABLE)")

    # 14. v4.2: ALTER_COLUMN_TYPE trigger propagates UPDATEs on backfilled rows (TG_OP = 'UPDATE' + IS DISTINCT FROM)
    op_at = parse_migration("prisma", 'ALTER TABLE "Items" ALTER COLUMN "qty" TYPE INTEGER;', "prisma/migrations/5/migration.sql")[0]
    p_at = synthesize_expand_contract(op_at, old_pg_type="TEXT", new_pg_type="INTEGER")
    trig_sql = "\n".join(p_at.phase1_expand_sql)
    assert "TG_OP = 'UPDATE'" in trig_sql and "IS DISTINCT FROM OLD." in trig_sql, trig_sql
    print("PASS Test 14 (ALTER_COLUMN_TYPE trigger handles UPDATE sync)")


if __name__ == "__main__":
    run_tests()
