from __future__ import annotations

import os
from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "scripts/deploy/macos/deploy.sh"
PREFLIGHT = REPO_ROOT / "scripts/deploy/macos/preflight.sh"
DEPLOY_ENV_EXAMPLE = REPO_ROOT / "scripts/deploy/macos/deploy.local.env.example"
RUNTIME_RUNNER = REPO_ROOT / "scripts/execution_control/run_alembic_upgrade.sh"
FACT_RUNNER = REPO_ROOT / "scripts/execution_control/run_fact_alembic_upgrade.sh"
INSTALL_LAUNCH_AGENTS = REPO_ROOT / "scripts/execution_control/install_launch_agents.sh"
EXECUTOR_ENV_EXAMPLE = REPO_ROOT / "scripts/execution_control/executor.local.env.example"
LAUNCHD_DIR = REPO_ROOT / "config/deployment/launchd"
SKILL_ENV_EXAMPLE = REPO_ROOT / "skills/mujitask-tiktok-feishu-sync/skill.local.env.example"
ARCHITECTURE_OWNERSHIP = REPO_ROOT / "contracts/harness/architecture-ownership.yaml"
CODE_ROADMAP = REPO_ROOT / "contracts/harness/code-roadmap.yaml"
FACT_SCHEMA_DESIGN = REPO_ROOT / "docs/arch/fact-db-schema-design.md"
WORKFLOW_DESIGN = REPO_ROOT / "docs/arch/workflow-amazon-product-detail-design.md"
DEPLOYMENT_DOC = REPO_ROOT / "docs/ops/deployment.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _call_deploy_function(
    shell_body: str,
    *,
    args: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = 'source "$1"; PYTHON_BIN="$(command -v python3)"; ' + shell_body
    return subprocess.run(
        ["bash", "-c", command, "bash", str(DEPLOY), *args],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_merge_key_value_file_removes_duplicate_managed_lines(tmp_path: Path) -> None:
    env_file = tmp_path / "duplicate.env"
    env_file.write_text(
        "MANAGED=stale-first\n"
        "UNMANAGED=keep\n"
        "export MANAGED=stale-last\n",
        encoding="utf-8",
    )

    result = _call_deploy_function(
        'merge_key_value_file "$2" "MANAGED=current"',
        args=(str(env_file),),
    )

    assert result.returncode == 0, result.stderr
    assert env_file.read_text(encoding="utf-8") == (
        "MANAGED=current\nUNMANAGED=keep\n"
    )


def test_sql_literal_escapes_quotes_and_backslashes_as_an_escape_string() -> None:
    env = os.environ.copy()
    env["TEST_SQL_LITERAL"] = "back\\slash'quote"

    result = _call_deploy_function('sql_literal "$TEST_SQL_LITERAL"', env=env)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "E'back\\\\slash''quote'"


def test_macos_deploy_orders_runtime_then_fact_migration_before_launchd() -> None:
    source = _read(DEPLOY)
    identity_gate = (
        'verify_database_identity "Fact" "${fact_db_url}" '
        '"${fact_migration_db_url}"'
    )
    runtime_migration = 'bash "${install_dir}/scripts/execution_control/run_alembic_upgrade.sh"'
    native_bootstrap = 'bootstrap_native_legacy_schemas "${db_url}"'
    fact_migration = 'bash "${install_dir}/scripts/execution_control/run_fact_alembic_upgrade.sh"'
    fact_grant = (
        'grant_native_fact_runtime_compatibility "${fact_migration_db_url}" '
        '"${fact_runtime_role}"'
    )
    privilege_gate = 'verify_fact_runtime_privileges "${fact_db_url}" "${fact_runtime_role}"'
    migration_secret_cleanup = 'rm -f -- "${migration_env_file}"'
    launchd = 'bash "${install_dir}/scripts/execution_control/install_launch_agents.sh"'

    assert source.index(identity_gate) < source.index(runtime_migration)
    assert source.index(runtime_migration) < source.index(native_bootstrap)
    assert source.index(runtime_migration) < source.index(fact_migration)
    assert source.index(fact_migration) < source.index(fact_grant)
    assert source.index(fact_grant) < source.index(privilege_gate)
    assert source.index(fact_migration) < source.index(privilege_gate)
    assert source.index(privilege_gate) < source.index(migration_secret_cleanup)
    assert source.index(migration_secret_cleanup) < source.index(launchd)
    assert source.index(privilege_gate) < source.index(launchd)
    assert "MUJITASK_SKIP_SCHEMA_BOOTSTRAP" not in source
    assert "MUJITASK_SKIP_SCHEMA_BOOTSTRAP" not in _read(INSTALL_LAUNCH_AGENTS)


def test_external_runtime_migration_uses_private_url_and_identity_gate() -> None:
    deploy = _read(DEPLOY)
    preflight = _read(PREFLIGHT)
    example = _read(DEPLOY_ENV_EXAMPLE)
    runner = _read(RUNTIME_RUNNER)
    main = deploy[deploy.index("main() {") :]

    assert "require_config_value MUJITASK_RUNTIME_MIGRATION_DB_URL" in preflight
    assert "MUJITASK_RUNTIME_MIGRATION_DB_URL=" in example
    assert 'configured_runtime_migration_db_url="${MUJITASK_RUNTIME_MIGRATION_DB_URL:-}"' in main
    assert 'runtime_migration_db_url="${configured_runtime_migration_db_url}"' in main
    assert (
        'verify_database_identity "Runtime" "${db_url}" '
        '"${runtime_migration_db_url}"' in main
    )
    assert "BUSINESS_EXECUTION_CONTROL_RUNTIME_MIGRATION_DB_URL" in main
    assert "BUSINESS_EXECUTION_CONTROL_RUNTIME_MIGRATION_DB_URL" in runner
    assert 'BUSINESS_EXECUTION_CONTROL_DB_URL="${runtime_migration_db_url}"' in runner
    assert runner.index('runtime_migration_db_url="${BUSINESS_EXECUTION_CONTROL_') < runner.index(
        'source "${ENV_FILE}"'
    ) < runner.index('BUSINESS_EXECUTION_CONTROL_DB_URL="${runtime_migration_db_url}"')
    assert "MUJITASK_RUNTIME_MIGRATION_DB_URL" not in _read(EXECUTOR_ENV_EXAMPLE)


def test_external_launchd_install_never_bootstraps_runtime_or_tiktok_schema() -> None:
    source = _read(DEPLOY)
    external_installer = source[
        source.index("install_external_launch_agents() {") : source.index(
            "install_agent_skill() {"
        )
    ]
    main = source[source.index("main() {") :]

    assert "RuntimeStore" not in external_installer
    assert "TKFactStore" not in external_installer
    assert "bootstrap_schema" not in external_installer
    assert 'if [[ "${MUJITASK_RUNTIME_MODE:-native}" == "external" ]]' in main
    assert 'install_external_launch_agents "${install_dir}"' in main
    assert 'bash "${install_dir}/scripts/execution_control/install_launch_agents.sh"' in main
    assert main.index('install_external_launch_agents "${install_dir}"') < main.index(
        'bash "${install_dir}/scripts/execution_control/install_launch_agents.sh"'
    )


def test_executor_runtime_env_is_sealed_before_launchd() -> None:
    source = _read(DEPLOY)
    target_guard = source[
        source.index("validate_private_file_target() {") : source.index(
            "seal_private_file() {"
        )
    ]
    final_guard = source[
        source.index("seal_private_file() {") : source.index(
            "write_fact_migration_env() {"
        )
    ]
    main = source[source.index("main() {") :]

    assert '[[ ! -L "${file_path}" ]]' in target_guard
    assert "must be owned by the current user" in target_guard
    assert 'chmod 600 "${file_path}"' in final_guard
    assert '[[ "${file_mode}" == "600" ]]' in final_guard
    assert "ownership changed unexpectedly" in final_guard
    assert main.index(
        'validate_private_file_target "${executor_env_file}" '
        '"Executor environment file"'
    ) < main.index("write_executor_local_env \\")
    write_index = main.index("write_executor_local_env \\")
    fact_merge_index = main.index('"BUSINESS_EXECUTION_CONTROL_FACT_DB_URL=$(quote_env_value')
    first_seal_index = main.index(
        'seal_private_file "${executor_env_file}" "Executor environment file"'
    )
    final_seal_index = main.rindex(
        'seal_private_file "${executor_env_file}" "Executor environment file"'
    )
    migration_env_index = main.index("local migration_env_file=")
    assert write_index < first_seal_index < fact_merge_index
    assert fact_merge_index < final_seal_index < migration_env_index


def test_all_deployed_secret_files_use_the_generic_private_file_guard() -> None:
    source = _read(DEPLOY)
    minio = source[
        source.index("ensure_native_minio() {") : source.index(
            "start_runtime_services() {"
        )
    ]
    skill = source[
        source.index("install_agent_skill() {") : source.index("main() {")
    ]
    main = source[source.index("main() {") :]

    assert "validate_private_file_target()" in source
    assert "seal_private_file()" in source
    assert (
        'validate_private_file_target "${plist_path}" "MinIO launchd plist"'
        in minio
    )
    assert 'seal_private_file "${plist_path}" "MinIO launchd plist"' in minio
    assert (
        'validate_private_file_target "${skill_env_file}" "Skill environment file"'
        in skill
    )
    assert 'seal_private_file "${skill_env_file}" "Skill environment file"' in skill
    assert (
        'validate_private_file_target "${executor_env_file}" '
        '"Executor environment file"' in main
    )
    assert (
        'seal_private_file "${executor_env_file}" "Executor environment file"'
        in main
    )


def test_deploy_keeps_database_credentials_out_of_child_process_arguments() -> None:
    source = _read(DEPLOY)
    wait_for_runtime = source[
        source.index("wait_for_runtime() {") : source.index(
            "validate_private_file_target() {"
        )
    ]
    fact_identity = source[
        source.index("verify_database_identity() {") : source.index(
            "bootstrap_native_legacy_schemas() {"
        )
    ]
    native_bootstrap = source[
        source.index("bootstrap_native_legacy_schemas() {") : source.index(
            "grant_native_fact_runtime_compatibility() {"
        )
    ]
    fact_grant = source[
        source.index("grant_native_fact_runtime_compatibility() {") : source.index(
            "verify_fact_runtime_privileges() {"
        )
    ]
    fact_gate = source[
        source.index("verify_fact_runtime_privileges() {") : source.index(
            "install_external_launch_agents() {"
        )
    ]

    assert "quote(sys.stdin.read()" in source
    assert "shlex.quote(sys.stdin.read())" in source
    assert "MUJITASK_DEPLOY_RUNTIME_DB_URL" in wait_for_runtime
    assert "MUJITASK_DEPLOY_WORKER_DB_URL" in fact_identity
    assert "MUJITASK_DEPLOY_MIGRATION_DB_URL" in fact_identity
    assert "MUJITASK_DEPLOY_RUNTIME_DB_URL" in native_bootstrap
    assert "MUJITASK_DEPLOY_FACT_MIGRATION_DB_URL" in fact_grant
    assert "MUJITASK_DEPLOY_FACT_DB_URL" in fact_gate
    assert '"${venv_python}" - "${fact_db_url}"' not in source
    assert '"${venv_python}" - "${migration_db_url}"' not in source
    assert '-c "ALTER ROLE' not in source
    assert '-c "CREATE ROLE' not in source
    assert "spec_from_file_location" not in source
    assert "PASSWORD '${db_password_sql}'" not in source
    assert "PASSWORD '${fact_runtime_password_sql}'" not in source
    assert "rolname = ${db_user_sql}" in source
    assert "rolname = ${fact_runtime_role_sql}" in source
    assert "datname = ${db_name_sql}" in source
    assert "PASSWORD ${db_password_sql}" in source
    assert "PASSWORD ${fact_runtime_password_sql}" in source


def test_fact_migration_secret_is_cleaned_on_success_and_exit() -> None:
    source = _read(DEPLOY)
    cleanup = source[
        source.index("cleanup_deploy_files() {") : source.index(
            "# Keep managed configuration values"
        )
    ]
    migration_writer = source[
        source.index("write_fact_migration_env() {") : source.index(
            "verify_database_identity() {"
        )
    ]

    assert "trap cleanup_deploy_files EXIT" in source
    assert 'rm -f -- "${FACT_MIGRATION_ENV_FILE_TO_CLEAN}"' in cleanup
    assert 'FACT_MIGRATION_ENV_FILE_TO_CLEAN="${temp_file}"' in migration_writer
    assert 'FACT_MIGRATION_ENV_FILE_TO_CLEAN="${env_file}"' in migration_writer
    assert 'rm -f -- "${migration_env_file}"' in source
    assert 'FACT_MIGRATION_ENV_FILE_TO_CLEAN=""' in source


def test_cleanup_paths_cannot_be_injected_by_environment_file() -> None:
    source = _read(DEPLOY)
    main = source[source.index("main() {") :]

    assert source.index("unset TMP_ROOT") < source.index(
        'source "${SOURCE_DIR}/examples/openclaw/openclaw_deploy_common.sh"'
    )
    assert "readonly TMP_ROOT" in source
    assert main.index("load_deploy_env") < main.index(
        'FACT_MIGRATION_ENV_FILE_TO_CLEAN=""'
    )
    assert main.index("load_deploy_env") < main.index(
        "unset MUJITASK_RUNTIME_MIGRATION_DB_URL"
    )


def test_deploy_keeps_migration_credentials_out_of_worker_sources() -> None:
    deploy = _read(DEPLOY)
    executor_example = _read(EXECUTOR_ENV_EXAMPLE)
    launchd_sources = "\n".join(
        _read(path) for path in sorted(LAUNCHD_DIR.glob("*.plist.template"))
    )

    assert "runtime/deployment/migration.local.env" in deploy
    assert 'chmod 600 "${env_file}"' in deploy
    assert '"TK_FACT_DB_URL"' in deploy
    assert '"BUSINESS_EXECUTION_CONTROL_FACT_MIGRATION_DB_URL"' in deploy
    assert '"BUSINESS_EXECUTION_CONTROL_FACT_DB_URL=$(quote_env_value "${fact_db_url}")"' in deploy
    migration_writer = deploy[
        deploy.index("write_fact_migration_env() {") : deploy.index(
            "verify_database_identity() {"
        )
    ]
    assert "BUSINESS_EXECUTION_CONTROL_FACT_MIGRATION_DB_URL" in migration_writer
    assert "BUSINESS_EXECUTION_CONTROL_FACT_RUNTIME_ROLE" in migration_writer
    assert "BUSINESS_EXECUTION_CONTROL_MIGRATION_DB_URL" not in migration_writer
    assert "must not be a symlink" in migration_writer
    assert "mktemp" in migration_writer
    assert 'mv -f "${temp_file}" "${env_file}"' in migration_writer
    for forbidden in (
        "BUSINESS_EXECUTION_CONTROL_MIGRATION_DB_URL",
        "BUSINESS_EXECUTION_CONTROL_FACT_MIGRATION_DB_URL",
        "BUSINESS_EXECUTION_CONTROL_FACT_RUNTIME_ROLE",
    ):
        assert forbidden not in executor_example
        assert forbidden not in launchd_sources
    install_skill = deploy[
        deploy.index("install_agent_skill() {") : deploy.index("main() {")
    ]
    for forbidden_skill_key in (
        "MUJITASK_RUNTIME_MIGRATION_DB_URL",
        "MUJITASK_FACT_MIGRATION_DB_URL",
        "MUJITASK_FACT_RUNTIME_ROLE",
        "MUJITASK_FACT_RUNTIME_PASSWORD",
        "BUSINESS_EXECUTION_CONTROL_RUNTIME_MIGRATION_DB_URL",
        "BUSINESS_EXECUTION_CONTROL_FACT_MIGRATION_DB_URL",
    ):
        assert f'"{forbidden_skill_key}"' in install_skill


def test_fact_runner_never_loads_executor_env_and_requires_private_inputs() -> None:
    fact_runner = _read(FACT_RUNNER)
    runtime_runner = _read(RUNTIME_RUNNER)

    assert "executor.local.env" not in fact_runner
    assert "BUSINESS_EXECUTION_CONTROL_MIGRATION_ENV_FILE" in fact_runner
    assert "BUSINESS_EXECUTION_CONTROL_FACT_MIGRATION_DB_URL is required" in fact_runner
    assert "BUSINESS_EXECUTION_CONTROL_FACT_RUNTIME_ROLE is required" in fact_runner
    assert "MIGRATION_ENV_MODE" in fact_runner
    assert "executor.local.env" in runtime_runner
    assert "BUSINESS_EXECUTION_CONTROL_MIGRATION_ENV_FILE" not in runtime_runner


def test_fact_migration_runner_requires_an_explicit_private_env_file() -> None:
    env = os.environ.copy()
    env.pop("BUSINESS_EXECUTION_CONTROL_MIGRATION_ENV_FILE", None)
    env["BUSINESS_EXECUTION_CONTROL_FACT_MIGRATION_DB_URL"] = (
        "postgresql+psycopg://ignored"
    )

    result = subprocess.run(
        ["bash", str(FACT_RUNNER)],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "BUSINESS_EXECUTION_CONTROL_MIGRATION_ENV_FILE is required" in result.stderr


def test_fact_runner_rejects_group_readable_migration_env(tmp_path: Path) -> None:
    migration_env = tmp_path / "migration.local.env"
    migration_env.write_text(
        "BUSINESS_EXECUTION_CONTROL_FACT_MIGRATION_DB_URL=postgresql+psycopg://unused\n"
        "BUSINESS_EXECUTION_CONTROL_FACT_RUNTIME_ROLE=fact_runtime\n",
        encoding="utf-8",
    )
    migration_env.chmod(0o640)
    env = os.environ.copy()
    env["BUSINESS_EXECUTION_CONTROL_MIGRATION_ENV_FILE"] = str(migration_env)

    result = subprocess.run(
        ["bash", str(FACT_RUNNER)],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "must have mode 400 or 600" in result.stderr


def test_deploy_rejects_group_readable_high_privilege_config(tmp_path: Path) -> None:
    deploy_env = tmp_path / "deploy.local.env"
    deploy_env.write_text("MUJITASK_RUNTIME_MODE=native\n", encoding="utf-8")
    deploy_env.chmod(0o640)
    env = os.environ.copy()
    env["MUJITASK_DEPLOY_ENV_FILE"] = str(deploy_env)

    result = subprocess.run(
        ["bash", str(DEPLOY)],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "must have mode 400 or 600" in result.stderr


def test_preflight_and_example_require_explicit_fact_runtime_identity() -> None:
    preflight = _read(PREFLIGHT)
    example = _read(DEPLOY_ENV_EXAMPLE)

    assert "require_config_value MUJITASK_FACT_RUNTIME_ROLE" in preflight
    assert "require_config_value MUJITASK_FACT_RUNTIME_PASSWORD" in preflight
    assert "require_config_value MUJITASK_FACT_DB_URL" in preflight
    assert "require_config_value MUJITASK_FACT_MIGRATION_DB_URL" in preflight
    assert 'MUJITASK_FACT_RUNTIME_ROLE="mujitask_fact_runtime"' in example
    assert "MUJITASK_FACT_RUNTIME_PASSWORD=" in example
    assert "MUJITASK_FACT_MIGRATION_DB_URL=" in example
    assert "chmod 600 scripts/deploy/macos/deploy.local.env" in example


def test_native_deploy_preserves_legacy_runtime_bootstrap_and_restricts_fact_role() -> None:
    deploy = _read(DEPLOY)

    assert 'createdb_bin}" -O "${db_user}" "${db_name}"' in deploy
    assert "bootstrap_native_legacy_schemas()" in deploy
    assert "runtime_store.bootstrap_schema()" in deploy
    assert "TKFactStore(runtime_store=runtime_store).bootstrap_schema()" in deploy
    assert "REASSIGN OWNED" not in deploy
    assert "grant_native_runtime_privileges" not in deploy
    assert "verify_runtime_privileges" not in deploy
    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS" in deploy
    assert 'REVOKE CREATE ON SCHEMA public FROM PUBLIC' in deploy


def test_fact_privilege_gate_checks_identity_tk_tables_and_actual_revision() -> None:
    deploy = _read(DEPLOY)
    identity_gate = deploy[
        deploy.index("verify_database_identity() {") : deploy.index(
            "bootstrap_native_legacy_schemas() {"
        )
    ]
    fact_grant = deploy[
        deploy.index("grant_native_fact_runtime_compatibility() {") : deploy.index(
            "verify_fact_runtime_privileges() {"
        )
    ]
    fact_gate = deploy[
        deploy.index("verify_fact_runtime_privileges() {") : deploy.index("install_agent_skill() {")
    ]

    assert "current_database() AS database_name" in identity_gate
    assert "database_oid" in identity_gate
    assert "pg_postmaster_start_time" in identity_gate
    assert "worker and migration URLs must resolve to the same running" in identity_gate
    assert '"PostgreSQL instance and database."' in identity_gate
    assert "AMAZON_FACT_SCHEMA_REVISION" in fact_gate
    assert "TK_FACT_SCHEMA_STATEMENTS" in fact_grant
    assert "TK_FACT_SCHEMA_STATEMENTS" in fact_gate
    assert 'statement))' in fact_grant
    assert "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA" in fact_grant
    assert "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA" in fact_grant
    assert "[*AMAZON_FACT_TABLES, *TK_FACT_TABLES]" in fact_gate
    assert "missing_tk_tables" in fact_gate
    assert "unexpected_table_privileges" in fact_gate
    assert "outside governed Fact tables" in fact_gate
    assert "pg_namespace" in fact_gate
    assert "nspname !~ '^pg_'" in fact_gate
    assert "information_schema" in fact_gate
    assert "c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')" in fact_gate
    assert "has_sequence_privilege" in fact_gate
    assert "has_any_column_privilege" in fact_gate
    assert "table_has_privilege" in fact_gate
    assert "lacks USAGE on the Fact schema" in fact_gate
    assert "owns schema relations" in fact_gate
    assert "SELECT version_num FROM" in fact_gate
    assert "Fact schema revision mismatch" in fact_gate


def test_shared_fact_role_contract_excludes_runtime_tables() -> None:
    architecture_ownership = _read(ARCHITECTURE_OWNERSHIP)
    code_roadmap = _read(CODE_ROADMAP)
    fact_schema_design = _read(FACT_SCHEMA_DESIGN)
    workflow_design = _read(WORKFLOW_DESIGN)
    deployment_doc = _read(DEPLOYMENT_DOC)

    assert "governed TikTok and Amazon Fact table DML" in architecture_ownership
    assert "governed TikTok and Amazon Fact table DML" in code_roadmap
    assert "it receives no Runtime table privileges" in code_roadmap
    assert "不得拥有 Runtime 表权限" in fact_schema_design
    assert "不得授予 Runtime 表权限" in workflow_design
    assert "不含 Runtime 表权限或 DDL" in deployment_doc


def test_deploy_writes_amazon_route_to_skill_and_worker_runtime() -> None:
    deploy = _read(DEPLOY)
    preflight = _read(PREFLIGHT)
    deploy_example = _read(DEPLOY_ENV_EXAMPLE)
    skill_example = _read(SKILL_ENV_EXAMPLE)
    executor_example = _read(EXECUTOR_ENV_EXAMPLE)
    launchd_sources = "\n".join(
        _read(path) for path in sorted(LAUNCHD_DIR.glob("*.plist.template"))
    )
    install_skill = deploy[deploy.index("install_agent_skill() {") : deploy.index("main() {")]
    skill_merge = install_skill[
        install_skill.index("merge_key_value_file") : install_skill.index(
            'INSTALLED_SKILL_DIR="${target_skill_dir}"'
        )
    ]

    assert 'require_feishu_table_route "AMAZON_PRODUCTS"' in preflight
    assert "require_config_value MUJITASK_AMAZON_US_BROWSER_PROFILE_REF" in preflight
    for key in (
        "MUJITASK_FEISHU_AMAZON_PRODUCTS_TABLE_ID",
        "MUJITASK_FEISHU_AMAZON_PRODUCTS_VIEW_ID",
    ):
        assert key in deploy_example
        assert key in skill_example
        assert key in skill_merge
    assert "MUJITASK_AMAZON_US_BROWSER_PROFILE_REF" in deploy_example
    assert "AMAZON_US_BROWSER_PROFILE_REF" not in skill_example
    assert "AMAZON_US_BROWSER_PROFILE_REF" not in skill_merge
    assert "AMAZON_US_BROWSER_PROFILE_REF" not in launchd_sources
    assert (
        '"AMAZON_US_BROWSER_PROFILE_REF=$(quote_env_value '
        '"${amazon_us_browser_profile_ref}")"' in deploy
    )
    executor_merge = deploy[
        deploy.index("write_executor_local_env \\") : deploy.index("local migration_env_file=")
    ]
    for key in (
        "MUJITASK_FEISHU_BASE_URL",
        "MUJITASK_FEISHU_AMAZON_PRODUCTS_TABLE_ID",
        "MUJITASK_FEISHU_AMAZON_PRODUCTS_VIEW_ID",
    ):
        assert key in executor_merge
        assert key in executor_example
    assert "BUSINESS_EXECUTION_CONTROL_FACT_DB_URL" in executor_example
    assert "TK_FACT_DB_URL" not in executor_example


def test_changed_deployment_shell_scripts_are_syntax_valid() -> None:
    for script in (
        DEPLOY,
        PREFLIGHT,
        RUNTIME_RUNNER,
        FACT_RUNNER,
        INSTALL_LAUNCH_AGENTS,
    ):
        subprocess.run(["bash", "-n", str(script)], check=True, cwd=REPO_ROOT)
