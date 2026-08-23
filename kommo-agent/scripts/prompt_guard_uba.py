#!/usr/bin/env python3
"""
Pre-deploy static guard for the kommo-agent engine.

Catches the class of bug that a syntax check (ast.parse) and general-purpose
linters (pyflakes, pylint) all miss: a function-level local NAME that is READ on
a line executing before its first ASSIGNMENT in the same function. This is the
exact failure that crashed talk=903 in production:

    any(i.get("scope") == "ready_to_proceed_agua" for i in _intents)
    ...
    _intents = await haiku_pre.classify(...)   # assigned LATER

A comprehension's LOOP TARGET (the 'x' in 'for x in seq') is scoped-local and is
ignored. A comprehension's ITERABLE and test expressions ARE real function-level
reads and ARE checked — that is the bug shape above (_intents is the iterable).

Usage:
    python3 prompt_guard_uba.py app/worker.py app/haiku.py app/state.py ...
Exit code 1 on any finding, 0 if clean — wire into the deploy cycle BEFORE
`docker commit`, right after the ast.parse syntax check.
"""
import ast
import sys


def _comp_target_names(fn):
    names = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp,
                             ast.GeneratorExp)):
            for gen in node.generators:
                for nm in ast.walk(gen.target):
                    if isinstance(nm, ast.Name):
                        names.add(nm.id)
    return names


def _except_locals(fn):
    return {n.name for n in ast.walk(fn)
            if isinstance(n, ast.ExceptHandler) and n.name}


def _assignments(fn, comp_targets):
    assigned = {}
    for a in fn.args.args + fn.args.kwonlyargs:
        assigned[a.arg] = fn.lineno
    if fn.args.vararg:
        assigned[fn.args.vararg.arg] = fn.lineno
    if fn.args.kwarg:
        assigned[fn.args.kwarg.arg] = fn.lineno
    for node in ast.walk(fn):
        ln = getattr(node, 'lineno', fn.lineno)
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        elif isinstance(node, ast.For):
            targets = [node.target]
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for it in node.items:
                if it.optional_vars:
                    targets.append(it.optional_vars)
        for t in targets:
            for nm in ast.walk(t):
                if isinstance(nm, ast.Name) and nm.id not in comp_targets:
                    if nm.id not in assigned or ln < assigned[nm.id]:
                        assigned[nm.id] = ln
    return assigned


def _analyze(fn):
    comp_targets = _comp_target_names(fn)
    exc = _except_locals(fn)
    assigned = _assignments(fn, comp_targets)
    ext = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            ext.update(node.names)
    findings = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            nm = node.id
            if nm in ext or nm in comp_targets or nm in exc:
                continue
            if nm not in assigned:
                continue
            if node.lineno < assigned[nm]:
                findings.append((node.lineno, nm, assigned[nm], fn.name))
    return findings


def main(paths):
    total = 0
    for path in paths:
        try:
            tree = ast.parse(open(path, encoding='utf-8').read())
        except SyntaxError as e:
            print(f"{path}: SYNTAX ERROR: {e}")
            total += 1
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for lineno, name, aln, fnname in _analyze(node):
                    print(f"{path}:{lineno}: '{name}' read before first "
                          f"assignment (first assigned line {aln}) in "
                          f"{fnname}()")
                    total += 1
    if total:
        print(f"\nGUARD FAILED — {total} use-before-assignment finding(s). "
              f"Do NOT deploy.")
        return 1
    print("GUARD PASSED — no use-before-assignment issues.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
