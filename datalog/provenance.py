"""推导来源链的遍历与文本格式化。"""

from __future__ import annotations

from .model import Atom, Derivation, FactProof, Result, repr_term


def walk_chosen(result: Result, predicate: str, args: tuple, _depth: int = 0):
    """沿字典序最小最短推导（``chosen``）自底向上之外的自顶向下展开。

    生成 ``(depth, predicate, args, rule_index, parents)`` 序列，
    前序（先结论后前提）。EDB 事实 ``rule_index=None``、无父节点。
    """
    proof = result.explain((predicate, tuple(args)))
    d = proof.chosen
    yield (_depth, predicate, tuple(args), d.rule_index, d.parents)
    for pname, pargs, positive in d.parents:
        if positive:
            yield from walk_chosen(result, pname, pargs, _depth + 1)
        else:
            # 否定见证：该事实在低层模型中不存在（闭世界），是叶子。
            yield (_depth + 1, pname, tuple(pargs), None, None)


def format_chain(result: Result, predicate: str, args: tuple) -> str:
    """把 chosen 推导 DAG 渲染为缩进树文本（含规则号与否定见证）。"""
    lines: list[str] = []
    for depth, name, row, rule_index, parents in walk_chosen(
            result, predicate, tuple(args)):
        indent = "  " * depth
        fact = f"{name}({', '.join(repr_term(c) for c in row)})"
        if rule_index is None and parents is None:
            lines.append(f"{indent}NOT {fact}  # 闭世界见证：该事实在低层模型中不存在")
        elif rule_index is None:
            lines.append(f"{indent}{fact}  # EDB 事实")
        else:
            rule = result.rules[rule_index]
            lines.append(f"{indent}{fact}  # 由规则[{rule_index}] {rule.format()}")
    return "\n".join(lines)


def all_facts_with_depth(result: Result, predicate: str):
    """返回某谓词全部 ``(args, 最短深度, 最短推导数)``，按 args 字典序。"""
    out = []
    for row in sorted(result.facts.get(predicate, frozenset()),
                      key=lambda r: tuple(str(x) for x in r)):
        p = result.proof[(predicate, row)]
        out.append((row, p.depth, len(p.derivations)))
    return out
