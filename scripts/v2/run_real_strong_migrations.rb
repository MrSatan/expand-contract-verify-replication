# Real strong_migrations 2.8.0 runner on verbatim Rails ActiveRecord migration files.
# Usage: ruby run_real_strong_migrations.rb <input_rails_cases.json> <output_results.json>

require "json"
require "active_record"
require "strong_migrations"

ActiveRecord::Migration.prepend(StrongMigrations::Migration) unless ActiveRecord::Migration.ancestors.include?(StrongMigrations::Migration)
StrongMigrations.start_after = 0
StrongMigrations.target_postgresql_version = "16"

class DummyPool
  def initialize(conn); @conn = conn; end
  def with_connection(&blk); blk.call(@conn); end
  def lease_connection; @conn; end
  def db_config; Struct.new(:name).new("primary"); end
end

class DummyTableDef
  def method_missing(m, *args, **kwargs, &blk)
    yield self if block_given?
    self
  end
  def respond_to_missing?(m, *_)
    ![:change, :up, :down].include?(m)
  end
end

class DummyPGAdapter
  attr_reader :ddl_calls
  def initialize
    @ddl_calls = []
  end
  def adapter_name; "PostgreSQL"; end
  def database_version; 160000; end
  def postgresql_version; 160000; end
  def table_exists?(*); true; end
  def data_source_exists?(*); true; end
  def column_exists?(*); true; end
  def index_exists?(*); false; end
  def foreign_key_exists?(*); false; end
  def check_constraint_exists?(*); false; end
  def columns(*); []; end
  def indexes(*); []; end
  def foreign_keys(*); []; end
  def check_constraints(*); []; end
  def primary_key(*); "id"; end
  def quote_column_name(c); "\"#{c}\""; end
  def quote_table_name(t); "\"#{t}\""; end
  def quote(v); "'#{v}'"; end
  def select_all(*); [{"server_version_num" => "160000", "lock_timeout" => "10s", "statement_timeout" => "60s"}]; end
  def select_value(*); "160000"; end
  def select_values(*); []; end
  def pool; @pool ||= DummyPool.new(self); end
  def transaction(&blk); blk.call if blk; end
  def supports_ddl_transactions?; true; end
  def open_transactions; 0; end
  def change_table(*args, **kwargs, &blk)
    @ddl_calls << [:change_table, args]
    yield DummyTableDef.new if block_given?
  end
  def create_table(*args, **kwargs, &blk)
    @ddl_calls << [:create_table, args]
    yield DummyTableDef.new if block_given?
  end
  def method_missing(m, *args, **kwargs, &blk)
    @ddl_calls << [m, args]
    yield DummyTableDef.new if block_given?
    nil
  end
  def respond_to_missing?(m, *_)
    ![:change, :up, :down].include?(m)
  end
end

# Stub common custom migration base classes used by large Rails repos (GitLab, Spree, Solidus, Discourse, Mastodon, etc.)
module StubbedMigrationBase
  def self.[](ver); ActiveRecord::Migration[7.1]; end
end

# Helper to preprocess custom superclass names into ActiveRecord::Migration[7.1] without altering migration body
def normalize_superclass(code)
  # Replace `class Foo < Something::Migration[x.y]` or `class Foo < ActiveRecord::Migration` with ActiveRecord::Migration[7.1]
  out = code.gsub(/(class\s+[A-Za-z0-9_:]+\s*<\s*)([A-Za-z0-9_:]+(?:\[[^\]]+\])?)/) do
    prefix = $1
    parent = $2
    if parent.include?("Migration") || parent.include?("ActiveRecord")
      "#{prefix}ActiveRecord::Migration[7.1]"
    else
      "#{prefix}#{parent}"
    end
  end
  # Convert legacy Rails 3 `def self.up` / `def self.down` to instance `def up` / `def down`
  out = out.gsub(/^\s*def\s+self\.(up|down)\b/, '  def \1')
  out
end

# Permissive dummy constant/module for unknown app models referenced inside data migrations
class DummyConst < BasicObject
  def self.method_missing(*, **, &blk); DummyConst; end
  def self.const_missing(_name); DummyConst; end
  def self.respond_to_missing?(*); true; end
  def self.[](*); ::ActiveRecord::Migration[7.1]; end
  def method_missing(*, **, &blk); DummyConst.new; end
  def respond_to_missing?(m, *_)
    ![:change, :up, :down].include?(m)
  end
end

in_path, out_path = ARGV
cases = JSON.parse(File.read(in_path))
results = []

# Suppress noisy ActiveRecord migration stdout
ActiveRecord::Migration.verbose = false

cases.each_with_index do |c, idx|
  cid = c["id"]
  code = c["verbatim_migration_content"].to_s
  t0 = Process.clock_gettime(Process::CLOCK_MONOTONIC)
  res = {
    "id" => cid,
    "executed_ok" => false,
    "flagged" => false,
    "error_kind" => nil,
    "message" => nil,
    "elapsed_ms" => 0.0,
  }

  if code.strip.empty?
    res["error_kind"] = "empty_file"
    results << res
    next
  end

  # Sandbox each migration in a fresh anonymous Module so class names never collide
  mod = Module.new do
    def self.const_missing(name)
      return ::ActiveRecord if name == :ActiveRecord
      return ::StrongMigrations if name == :StrongMigrations
      DummyConst
    end
  end

  conn = DummyPGAdapter.new
  ActiveRecord::Tasks::DatabaseTasks.define_singleton_method(:migration_connection) { conn }

  begin
    norm_code = normalize_superclass(code)
    # Strip `require` / `require_relative` lines that point to app internals
    norm_code = norm_code.gsub(/^\s*require(?:_relative)?\s+.*$/, "# stripped require")
    mod.module_eval(norm_code, c["file_path"] || "mig_#{idx}.rb", 1)

    mig_classes = mod.constants.map { |k| mod.const_get(k) }.select { |v| v.is_a?(Class) && v < ActiveRecord::Migration }
    if mig_classes.empty?
      res["error_kind"] = "no_migration_class"
    else
      m = mig_classes.first.new
      m.version = 20250101000000 + idx
      begin
        m.migrate(:up)
        res["executed_ok"] = true
        res["flagged"] = false
      rescue StrongMigrations::UnsafeMigration => e
        res["executed_ok"] = true
        res["flagged"] = true
        res["error_kind"] = "UnsafeMigration"
        res["message"] = e.message.lines.map(&:strip).reject(&:empty?).first(3).join(" | ")[0, 240]
      rescue => e
        # If an error happened AFTER or BEFORE StrongMigrations check, record cleanly
        res["executed_ok"] = false
        res["error_kind"] = e.class.name
        res["message"] = e.message.to_s[0, 220]
      end
    end
  rescue SyntaxError, StandardError => e
    res["executed_ok"] = false
    res["error_kind"] = e.class.name
    res["message"] = e.message.to_s[0, 220]
  end

  res["elapsed_ms"] = ((Process.clock_gettime(Process::CLOCK_MONOTONIC) - t0) * 1000.0).round(2)
  results << res
end

File.write(out_path, JSON.pretty_generate(results))
ok_cnt = results.count { |r| r["executed_ok"] }
flg_cnt = results.count { |r| r["flagged"] }
STDERR.puts "strong_migrations_v2.8.0: total=#{results.size} executed_ok=#{ok_cnt} flagged=#{flg_cnt}"
