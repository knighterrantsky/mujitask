from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "automation_business_scaffold"
TASKS_ROOT = SRC_ROOT / "business" / "tasks"
TASKS_INIT = SRC_ROOT / "business" / "tasks" / "__init__.py"
REGISTRY_MODULE = SRC_ROOT / "registry.py"
OFFICIAL_TASK_CODES = (
    "refresh_current_competitor_table",
    "search_keyword_competitor_products",
    "sync_tk_influencer_pool",
    "tiktok_fastmoss_product_ingest",
)


def _module_entry(root: Path, name: str) -> Path | None:
    module_file = root / f"{name}.py"
    if module_file.exists():
        return module_file

    package_init = root / name / "__init__.py"
    if package_init.exists():
        return package_init

    return None


def _defines_name(path: Path, expected_name: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == expected_name:
                    return True
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound_name = alias.asname or alias.name
                if bound_name == expected_name:
                    return True
    return False


def _load_registry_module(default_tasks: list[object]):
    class FakeTaskRegistry:
        def __init__(self) -> None:
            self.registered_batches: list[list[object]] = []

        def register_many(self, tasks) -> None:
            self.registered_batches.append(list(tasks))

    fake_framework = ModuleType("automation_framework")
    fake_framework_core = ModuleType("automation_framework.core")
    fake_framework_core.TaskRegistry = FakeTaskRegistry

    fake_root = ModuleType("automation_business_scaffold")
    fake_business = ModuleType("automation_business_scaffold.business")
    fake_tasks = ModuleType("automation_business_scaffold.business.tasks")
    fake_tasks.DEFAULT_TASKS = default_tasks
    fake_business.tasks = fake_tasks
    fake_root.business = fake_business

    module_names = (
        "automation_framework",
        "automation_framework.core",
        "automation_business_scaffold",
        "automation_business_scaffold.business",
        "automation_business_scaffold.business.tasks",
    )
    previous = {name: sys.modules.get(name) for name in module_names}
    sys.modules.update(
        {
            "automation_framework": fake_framework,
            "automation_framework.core": fake_framework_core,
            "automation_business_scaffold": fake_root,
            "automation_business_scaffold.business": fake_business,
            "automation_business_scaffold.business.tasks": fake_tasks,
        }
    )

    try:
        spec = importlib.util.spec_from_file_location("tests.registry_contract_subject", REGISTRY_MODULE)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, FakeTaskRegistry
    finally:
        for name, original in previous.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


def test_business_tasks_package_must_define_default_tasks() -> None:
    assert TASKS_INIT.exists(), "business.tasks package must exist and define DEFAULT_TASKS for registry discovery."
    assert _defines_name(TASKS_INIT, "DEFAULT_TASKS"), (
        "business.tasks.__init__ must define or re-export DEFAULT_TASKS so registry.py stays a thin shell."
    )


def test_business_tasks_package_exposes_the_four_formal_task_entry_modules() -> None:
    missing = [task_code for task_code in OFFICIAL_TASK_CODES if _module_entry(TASKS_ROOT, task_code) is None]
    assert missing == [], (
        "business.tasks should keep one discoverable task entry module per formal workflow:\n" + "\n".join(missing)
    )


def test_build_task_registry_registers_default_tasks_without_hardcoding_task_names() -> None:
    assert REGISTRY_MODULE.exists(), "registry.py must exist as the framework task discovery entrypoint."

    default_tasks = [
        SimpleNamespace(name=task_code)
        for task_code in OFFICIAL_TASK_CODES
    ]
    module, fake_registry_type = _load_registry_module(default_tasks)

    registry = module.build_task_registry()

    assert isinstance(registry, fake_registry_type)
    assert registry.registered_batches == [default_tasks], (
        "registry.py should forward business.tasks.DEFAULT_TASKS into TaskRegistry.register_many()."
    )
