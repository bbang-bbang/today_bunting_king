"""함수 안 conditional import 가 module-level 변수를 shadow 하는지 검사.

배경: 5/6 사고 — `cb_button` 함수 안에 `from src.db.connection import get_connection`
조건부 import 가 있어 Python 스코프 규칙으로 함수 전체에서 `get_connection` 이 local
변수가 됐고, 다른 분기에서 module-level 호출 시 UnboundLocalError. 매수 직전 ohlcv
조회가 실패해서 매수 의사결정이 부정확해질 수 있었음.

이 테스트는 같은 클래스 사고를 정적으로 차단한다.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


def _scan(py_path: Path) -> list[tuple[str, str, int]]:
    """반환: [(함수명, shadow 되는 변수명, conditional import 라인)]."""
    tree = ast.parse(py_path.read_text(encoding="utf-8"))

    # 모듈 최상위 from-import 이름
    module_names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                module_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                module_names.add(alias.asname or alias.name.split(".")[0])

    issues: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for inner in ast.walk(node):
            if inner is node:
                continue
            # 중첩 함수의 import 는 그 함수의 스코프이므로 부모 함수와 무관 — 스킵
            if isinstance(inner, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if isinstance(inner, ast.ImportFrom):
                for alias in inner.names:
                    name = alias.asname or alias.name
                    if name in module_names:
                        issues.append((node.name, name, inner.lineno))
            elif isinstance(inner, ast.Import):
                for alias in inner.names:
                    name = alias.asname or alias.name.split(".")[0]
                    if name in module_names:
                        issues.append((node.name, name, inner.lineno))
    return issues


@pytest.mark.parametrize("py_path", sorted(SRC.rglob("*.py")), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_import_shadow_in_functions(py_path: Path):
    """함수 안 conditional import 가 module-level 이름을 shadow 하면 안 됨.

    수정법: 1) 함수 안 import 제거 (module-level 만 사용), 또는
            2) 함수 안 import 만 사용 (module-level 제거), 또는
            3) `as` 로 다른 이름 부여 (예: `from X import Y as _Y`).
    """
    issues = _scan(py_path)
    if issues:
        msg = f"{py_path.relative_to(ROOT)} 에 import shadow 잠복:\n"
        for fn, name, lineno in issues:
            msg += f"  - 함수 {fn}() 안 line {lineno}: '{name}' 이 module-level 과 충돌 → UnboundLocalError 위험\n"
        pytest.fail(msg)
