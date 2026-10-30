"""Structural assertions that are about the CODE, not about its spelling.

Why this exists
---------------
97 of SEM's test files assert a structural fact by grepping a function's
source text::

    src = inspect.getsource(SEMCoordinator._shadow_energy_plan)
    assert "export_rate=" in src

That is one line, which is why it is everywhere — 121 of them. It is also
the shape that let #924 live for a month: the assertion names ONE function,
so three sibling call sites kept the old behaviour underneath it, and one of
those packed the plan a user reads. It is coupled to the SPELLING of the
code, not to the code. It cannot tell you:

* whether the call is REACHED (it may sit in a dead branch),
* whether siblings elsewhere lack it (the #924 defect exactly),
* whether a rename broke it (a renamed callee makes the test pass on the
  old string until someone notices),
* whether the string is in a COMMENT or a docstring rather than in code.

51 files already do it properly with ``ast``, and every one of them rolls
its own walker — thirty lines to say what the grep says in one. So the
fragile form wins on ergonomics, every time, and the ledger fills up.

This module makes the sound form one line too. Prefer these; reach for
``inspect.getsource`` only when the thing you are pinning genuinely IS text
(a comment, an annotation, a log message a user will read).

Everything here ignores strings, comments and docstrings by construction:
it walks the parsed tree, so a mention in prose is not a call.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from typing import Callable, Iterable, Optional


def _tree_of(fn: Callable) -> ast.AST:
    return ast.parse(textwrap.dedent(inspect.getsource(fn)))


def _callee_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def calls(fn: Callable, callee: str) -> bool:
    """Does ``fn`` contain a CALL to ``callee``? Matches a bare name or an
    attribute (``x.callee()``). A mention in a comment or a string is not a
    call, which is the whole point."""
    return any(isinstance(n, ast.Call) and _callee_name(n) == callee
               for n in ast.walk(_tree_of(fn)))


def call_kwargs(fn: Callable, callee: str) -> list:
    """Every call to ``callee`` inside ``fn``, as its list of keyword names.

    Use it to pin "this call passes X" without pinning how X is spelled::

        assert all("export_rate" in kw for kw in
                   call_kwargs(coord._shadow_energy_plan, "build_day_slots"))
    """
    out = []
    for n in ast.walk(_tree_of(fn)):
        if isinstance(n, ast.Call) and _callee_name(n) == callee:
            out.append([k.arg for k in n.keywords if k.arg])
    return out


def reads_attribute(fn: Callable, obj: str, attr: str) -> bool:
    """Does ``fn`` read ``obj.attr``? (``reads_attribute(f, "self", "_x")``)

    The #915 read-back was dead because three call sites read
    ``self._battery_adapter`` — a name nothing had assigned since #375. A
    grep for that string also matched a docstring and a longer sibling
    attribute; this does not."""
    for n in ast.walk(_tree_of(fn)):
        if (isinstance(n, ast.Attribute) and n.attr == attr
                and isinstance(n.value, ast.Name) and n.value.id == obj):
            return True
    return False


def reads_flag(fn: Callable, obj: str, attr: str) -> bool:
    """Does ``fn`` read ``obj.attr`` EITHER WAY — plainly, or through
    ``getattr(obj, "attr", default)``?

    (#1027) The house style for a flag that may be missing on a bare test
    stub is ``getattr(self, "observer_mode", False)``, and
    :func:`reads_attribute` sees no attribute there at all. A guard that
    pins "this gate asks whether SEM commands" has to accept both spellings,
    or it pins the spelling instead of the gate.
    """
    if reads_attribute(fn, obj, attr):
        return True
    for n in ast.walk(_tree_of(fn)):
        if not (isinstance(n, ast.Call) and _callee_name(n) == "getattr"):
            continue
        if len(n.args) < 2:
            continue
        target, name = n.args[0], n.args[1]
        if (isinstance(target, ast.Name) and target.id == obj
                and isinstance(name, ast.Constant) and name.value == attr):
            return True
    return False


def assigns_attribute(fn: Callable, obj: str, attr: str) -> bool:
    """Does ``fn`` ASSIGN ``obj.attr``? The counterpart to the above — a
    read with no writer anywhere is a dead feature (#915)."""
    for n in ast.walk(_tree_of(fn)):
        if not isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            continue
        targets = n.targets if isinstance(n, ast.Assign) else [n.target]
        for t in targets:
            if (isinstance(t, ast.Attribute) and t.attr == attr
                    and isinstance(t.value, ast.Name) and t.value.id == obj):
                return True
    return False


def call_sites(callee: str, *, root: Optional[Path] = None,
               skip_dirs: Iterable[str] = ("tests", "scripts",
                                           "node_modules", ".git")) -> list:
    """EVERY production call to ``callee`` in the package, as
    ``[(relative_path, lineno, [kwarg names])]``.

    This is the one that answers the #924 question — "are the SIBLINGS
    right?" — which no per-function assertion can ever ask. Write coverage
    rules over this, not over one function's source."""
    root = root or Path(__file__).resolve().parent.parent
    hits = []
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root)
        if set(rel.parts) & set(skip_dirs):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:          # a file we cannot parse is not a pass
            raise
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and _callee_name(n) == callee:
                hits.append((str(rel), n.lineno,
                             [k.arg for k in n.keywords if k.arg]))
    return hits


#: Directories a source contract never walks. Anything generated, vendored
#: or virtual belongs here: `_production_files` parses every file it reaches,
#: so one un-parseable tree would turn a contract into a crash rather than a
#: finding.
_SKIP_DIRS = ("tests", "scripts", "node_modules", ".git", "__pycache__",
              ".venv", "venv", "build", "dist", ".tox", ".mypy_cache")


def _rebinds(node: ast.AST, name: str) -> bool:
    """Does this statement rebind ``name``? A handler's exception name that
    has been overwritten is no longer evidence that anything was caught."""
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id == name and isinstance(
                n.ctx, (ast.Store, ast.Del)):
            return True
    return False


#: Bodies Python evaluates in their OWN scope. An ``except … as e`` name is
#: deleted at handler exit, so a closure written inside the handler and called
#: later sees nothing — the binding must not leak into these.
_OWN_SCOPE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
              ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _except_bound_names(tree: ast.AST) -> dict:
    """``{id(Call node): frozenset(names an enclosing except-as has bound)}``.

    Walks handlers rather than the whole tree so the binding is SCOPED: a
    name caught three functions away does not count, a name the handler
    reassigned no longer counts, and a name referenced from a nested
    function or comprehension does not count either (Python has deleted it
    by then). ``except*`` groups bind exactly the same way."""
    bound: dict = {}

    def walk(node, names: frozenset):
        if isinstance(node, _OWN_SCOPE):
            names = frozenset()
        if isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))) \
                and hasattr(node, "handlers"):
            for part in (node.body, node.orelse, node.finalbody):
                for st in part:
                    walk(st, names)
            for h in node.handlers:
                if h.type is not None:
                    walk(h.type, names)
                inner = names | ({h.name} if h.name else set())
                for st in h.body:
                    walk(st, frozenset(inner))
                    if h.name and _rebinds(st, h.name):
                        inner = set(inner) - {h.name}
            return
        if isinstance(node, ast.Call):
            bound[id(node)] = names
        for child in ast.iter_child_nodes(node):
            walk(child, names)

    walk(tree, frozenset())
    return bound


def _production_files(root: Optional[Path], skip_dirs: Iterable[str]):
    root = root or Path(__file__).resolve().parent.parent
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root)
        if set(rel.parts) & set(skip_dirs):
            continue
        yield rel, ast.parse(p.read_text(encoding="utf-8"))


def invented_evidence_call_sites(
        callee: str, *, root: Optional[Path] = None,
        skip_dirs: Iterable[str] = _SKIP_DIRS) -> list:
    """(#945, bug class 86) Every production call to ``callee`` whose
    evidence argument is an exception SEM made up, rather than one an
    enclosing ``except … as e`` actually caught.

    This is the class-86 sweep question asked structurally, for any counter
    that takes an exception as its evidence. A caught name means something
    really happened and really refused; ``RuntimeError("the switch is off")``
    means somebody turned an OBSERVATION into the same verdict — which is
    how a restart's warm-up became "your last 3+ commands were rejected".

    Checks the first positional AND every keyword argument, because the
    parameter has a name and the keyword form is the natural spelling. Pair
    it with :func:`symbol_reference_files` — a call reached through
    ``getattr(obj, "callee")`` is invisible here BY NAME, which is exactly
    how the bug was written.

    Returns ``[(relative_path, lineno, what_was_constructed)]``."""
    hits = []
    for rel, tree in _production_files(root, skip_dirs):
        bound = _except_bound_names(tree)
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and _callee_name(n) == callee):
                continue
            args = list(n.args[:1]) + [k.value for k in n.keywords]
            for arg in args:
                if isinstance(arg, ast.Name):
                    if arg.id in bound.get(id(n), frozenset()):
                        continue      # a command really raised — evidence
                    hits.append((str(rel), n.lineno, arg.id))
                elif isinstance(arg, ast.Call):
                    hits.append((str(rel), n.lineno, _callee_name(arg)))
                else:
                    hits.append((str(rel), n.lineno, type(arg).__name__))
    return hits


def symbol_reference_files(name: str, *, root: Optional[Path] = None,
                           skip_dirs: Iterable[str] = _SKIP_DIRS) -> list:
    """Every production file that so much as NAMES ``name`` in code — as a
    definition, an attribute access, or the string inside a ``getattr``.

    The companion to :func:`invented_evidence_call_sites`, and the reason
    it is not enough on its own: this codebase reaches its hooks through
    ``getattr(dev, "_record_actuation_failure", None)`` and then calls the
    local, so every callee-name contract is blind to the one shape the bug
    actually used. Answer "who may even MENTION this?" instead — a question
    indirection cannot dodge.

    Returns ``[(relative_path, lineno)]``, ignoring comments and docstrings
    by construction (bare string constants are not scanned)."""
    hits = []
    for rel, tree in _production_files(root, skip_dirs):
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and n.attr == name:
                hits.append((str(rel), n.lineno))
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and n.name == name:
                hits.append((str(rel), n.lineno))
            elif isinstance(n, ast.Call) and _callee_name(n) == "getattr":
                for a in n.args[1:2]:
                    if isinstance(a, ast.Constant) and a.value == name:
                        hits.append((str(rel), n.lineno))
    return sorted(set(hits))


def _mentions_states_get(node: ast.AST) -> bool:
    """Does this subtree CALL ``<something>.states.get(...)``?

    Narrow on purpose: a bare ``dict.get()`` is a mapping read and says
    nothing about HA's state machine. The receiver must be named ``states``
    — as an attribute (``hass.states.get``, the spelling this codebase uses
    everywhere) or as a bare name, which covers a hoisted
    ``states = hass.states``. The bare-name arm is deliberately wider than
    the attribute one: a local mapping called ``states`` would be flagged,
    and that false positive is cheaper than missing the hoisted spelling,
    because it only matters at all inside an ABSENCE branch that also nulls
    a handle — which is the bug either way.
    """
    for n in ast.walk(node):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "get"):
            continue
        recv = n.func.value
        if isinstance(recv, ast.Attribute) and recv.attr == "states":
            return True
        if isinstance(recv, ast.Name) and recv.id == "states":
            return True
    return False


def _is_absence_test(test: ast.AST) -> bool:
    """Is this condition asking whether a state read came back EMPTY?

    ``not hass.states.get(e)`` / ``hass.states.get(e) is None`` /
    ``... == None``. A POSITIVE test (``if hass.states.get(e):``) is the
    opposite shape and is not the class — acting on a reading you actually
    have is evidence, never silence.
    """
    if not _mentions_states_get(test):
        return False
    for n in ast.walk(test):
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            if _mentions_states_get(n.operand):
                return True
        if isinstance(n, ast.Compare) and _mentions_states_get(n.left):
            for op, cmp in zip(n.ops, n.comparators, strict=False):
                if (isinstance(op, (ast.Is, ast.Eq))
                        and isinstance(cmp, ast.Constant)
                        and cmp.value is None):
                    return True
    return False


def _null_target_names(target: ast.AST) -> list:
    """Every handle this assignment target discards, by the name a reader
    would recognise. Covers the four spellings a config handle is actually
    written in: a local (``entity = None``), an attribute
    (``device.current_entity_id = None``), a dict slot
    (``row["switch_entity"] = None`` — this tree keeps device rows in dicts,
    so it is the MOST natural way to re-acquire the class), and a tuple
    unpack."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Attribute):
        return [target.attr]
    if isinstance(target, ast.Subscript):
        sl = target.slice
        if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
            return [sl.value]
        base = target.value
        if isinstance(base, ast.Name):
            return [base.id]
        if isinstance(base, ast.Attribute):
            return [base.attr]
        return ["<subscript>"]
    if isinstance(target, (ast.Tuple, ast.List)):
        out = []
        for el in target.elts:
            out.extend(_null_target_names(el))
        return out
    return []


def _assigns_none(stmt: ast.AST) -> list:
    """``[(lineno, name)]`` for every ``= None`` this statement performs —
    plain, annotated, or the conditional-expression form
    (``e = None if states.get(e) is None else e``), which carries its own
    absence test and so is judged here rather than by an enclosing ``if``."""
    hits = []
    if isinstance(stmt, ast.Assign):
        targets, value = stmt.targets, stmt.value
    elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        targets, value = [stmt.target], stmt.value
    else:
        return hits
    nulls_unconditionally = (
        (isinstance(value, ast.Constant) and value.value is None)
        # ``entity, service = None, None`` — one statement, two handles.
        or (isinstance(value, (ast.Tuple, ast.List)) and bool(value.elts)
            and all(isinstance(el, ast.Constant) and el.value is None
                    for el in value.elts)))
    nulls_on_absence = (
        isinstance(value, ast.IfExp)
        and ((_is_absence_test(value.test)
              and isinstance(value.body, ast.Constant)
              and value.body.value is None)
             or (_mentions_states_get(value.test)
                 and isinstance(value.orelse, ast.Constant)
                 and value.orelse.value is None)))
    if not (nulls_unconditionally or nulls_on_absence):
        return hits
    for t in targets:
        for name in _null_target_names(t):
            hits.append((stmt.lineno, name, nulls_on_absence))
    return hits


def absence_spent_as_config(*, root: Optional[Path] = None,
                            skip_dirs: Iterable[str] = _SKIP_DIRS) -> list:
    """(#991, bug class 86) Every production site that DISCARDS a configured
    handle on the strength of a ``hass.states.get()`` absence.

    The class-86 sweep question, asked of the other consumer. #945 asked it
    of evidence COUNTERS ("is this observation evidence, or silence?"); this
    asks it of CONFIGURATION, where the same empty read is spent not on a
    verdict but on a capability — ``current_control_entity = None`` because
    an integration had not finished loading yet, permanent for the session
    because entity ids are structural and only a reload re-derives them.

    The shape: a condition that asks whether a state read is empty, and an
    assignment of ``None`` reached by it — as an ``if``/``elif`` body (at
    any nesting depth inside it) or as the conditional-expression form,
    which carries its own test. Targets cover a local, an attribute, a dict
    slot and a tuple unpack; plain and annotated assignment both. The answer
    to "is this entity there?" belongs where the entity is USED — a
    per-cycle read that may be wrong for one cycle and right for the next —
    never at setup, where the only honest answer is "I could not ask yet".

    **The limit, named so nobody mistakes a pass for proof:** the test and
    the assignment must be syntactically connected. The two-step dataflow
    form — ``st = hass.states.get(e)`` … later … ``if st is None: e = None``
    — is invisible here, and so is any spelling that hides the absence
    behind a helper predicate (``if self._dead(e): e = None``). That is not
    a gap this contract can close without dataflow; it is why the
    behavioural pin exists, driving the real registration with the entity
    absent. Both limits are pinned in
    ``tests/test_991_warmup_absence_not_spent.py`` so a future reader sees
    them rather than discovers them.

    Returns ``[(relative_path, lineno, what_was_nulled)]``."""
    hits = []
    for rel, tree in _production_files(root, skip_dirs):
        for n in ast.walk(tree):
            # (a) the conditional-expression form carries its own test, so
            # it is judged wherever it appears — no enclosing ``if`` needed.
            if isinstance(n, (ast.Assign, ast.AnnAssign)):
                for lineno, name, conditional in _assigns_none(n):
                    if conditional:
                        hits.append((str(rel), lineno, name))
                continue
            # (b) an absence branch, and anything it reaches. ``ast.walk``
            # over each body statement so a null nested in a ``try`` / ``for``
            # / ``with`` — or in a closure defined there — is not a hiding
            # place. ``elif`` is an ``If`` in ``orelse`` and is reached by
            # the same walk of the enclosing tree.
            if not (isinstance(n, ast.If) and _is_absence_test(n.test)):
                continue
            for stmt in n.body:
                for sub in ast.walk(stmt):
                    for lineno, name, conditional in _assigns_none(sub):
                        if not conditional:
                            hits.append((str(rel), lineno, name))
    return sorted(set(hits))


# ── (#1026, bug class 48) host registries read as a mapping ──────────────

#: The helper module whose ``async_get(hass)`` hands back HA's DEVICE registry.
_HOST_DEVICE_REGISTRY = "homeassistant.helpers.device_registry"

#: The containers on it that HA 2026.8 turned into a deprecation view. Reading
#: either as a mapping is reported now and removed in 2027.9.
_HOST_REGISTRY_MAPPINGS = ("devices", "deleted_devices")

#: Parameter names that mean "somebody handed me the host registry". A
#: contract cannot follow a value into a call, so the name has to carry it —
#: and these four are what this codebase spells it.
_REGISTRY_PARAM_NAMES = ("reg", "registry", "dev_reg", "device_reg")


def _dotted(node: ast.AST) -> str:
    """``self._dev_reg`` → ``"self._dev_reg"``; anything else → ``""``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else ""
    return ""


def _device_registry_names(tree: ast.AST) -> tuple:
    """What this file calls HA's ``device_registry`` helper.

    Returns ``(module_aliases, async_get_aliases)``. Both import styles count::

        from homeassistant.helpers import device_registry as dr   → dr.async_get
        import homeassistant.helpers.device_registry as dr        → dr.async_get
        from homeassistant.helpers.device_registry import async_get  → async_get

    The third one is why this is not just an alias set: it binds a BARE name,
    and a contract that only knew the dotted form skipped the whole file.
    """
    modules, getters = set(), set()
    head, _, tail = _HOST_DEVICE_REGISTRY.rpartition(".")
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            if n.module == head:
                for a in n.names:
                    if a.name == tail:
                        modules.add(a.asname or a.name)
            elif n.module == _HOST_DEVICE_REGISTRY:
                for a in n.names:
                    if a.name == "async_get":
                        getters.add(a.asname or a.name)
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name == _HOST_DEVICE_REGISTRY:
                    modules.add(a.asname or a.name)
    return modules, getters


def _is_registry_call(node: ast.AST, modules: set, getters: set) -> bool:
    """A call that hands back the host registry, in either import style."""
    if isinstance(node, ast.NamedExpr):       # (reg := dr.async_get(hass))
        node = node.value
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Attribute) and f.attr == "async_get":
        return _dotted(f.value) in modules
    return isinstance(f, ast.Name) and f.id in getters


def _registry_parameters(tree: ast.AST) -> list:
    """``(name, first_line, last_line)`` for every registry-named parameter.

    Scoped to the function that declares it, which is the whole point: SEM has
    its OWN registry and ``__init__.py`` passes it around under the same words.
    A file-wide rule read ``len(registry.devices)`` — our registry, 300 lines
    from the only function with a ``registry`` parameter — as a host read.
    """
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = fn.args
        for a in (list(args.posonlyargs) + list(args.args)
                  + list(args.kwonlyargs)):
            if a.arg in _REGISTRY_PARAM_NAMES:
                out.append((a.arg, fn.lineno, fn.end_lineno or fn.lineno))
    return out


def _bound_registry_names(tree: ast.AST, modules: set, getters: set) -> set:
    """Every name in this file that holds the host registry.

    Follows assignment, the walrus, tuple unpacking, ``for`` and ``with``
    targets and a plain rename (``r = reg``). Repeats until nothing new turns
    up, so a rename chain cannot outrun it. Parameters are handled separately,
    because they are only true inside their own function.
    """
    bound = set()

    def _mark(target, value) -> None:
        holds = (_is_registry_call(value, modules, getters)
                 or (isinstance(value, ast.Name) and value.id in bound)
                 or (isinstance(value, ast.NamedExpr)
                     and _is_registry_call(value.value, modules, getters)))
        if isinstance(target, (ast.Tuple, ast.List)) \
                and isinstance(value, (ast.Tuple, ast.List)) \
                and len(target.elts) == len(value.elts):
            for t, v in zip(target.elts, value.elts, strict=True):
                _mark(t, v)
            return
        if not holds:
            return
        name = _dotted(target)
        if name:
            bound.add(name)

    for _ in range(8):
        before = len(bound)
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    _mark(t, n.value)
            elif isinstance(n, ast.AnnAssign) and n.value is not None:
                _mark(n.target, n.value)
            elif isinstance(n, ast.NamedExpr):
                _mark(n.target, n.value)
            elif isinstance(n, (ast.For, ast.AsyncFor)):
                # `for reg in [dr.async_get(hass)]:` — the call is in there
                if any(_is_registry_call(sub, modules, getters)
                       for sub in ast.walk(n.iter)):
                    _mark(n.target, n.iter)
                    name = _dotted(n.target)
                    if name:
                        bound.add(name)
            elif isinstance(n, (ast.With, ast.AsyncWith)):
                for item in n.items:
                    if item.optional_vars is not None:
                        _mark(item.optional_vars, item.context_expr)
        if len(bound) == before:
            break
    return bound


def host_registry_mapping_reads(*, root: Optional[Path] = None,
                                skip_dirs: Iterable[str] = _SKIP_DIRS) -> list:
    """(#1026, bug class 48) Every production read of Home Assistant's device
    registry through one of its deprecated mapping views.

    HA 2026.8 made ``DeviceRegistry.devices`` a view that logs
    *"uses `device_registry.devices` as a mapping or calls its lookup methods"*
    for every custom integration and stops answering in 2027.9. The supported
    lookups are ``reg.async_get(device_id)`` and
    ``device_registry.async_entries_for_config_entry(reg, entry_id)``, both of
    which read the registry's real containers. Iterating ``reg.devices`` is
    allowed on 2026.8 but yields dict KEYS on the older versions SEM still
    supports, so this contract admits neither.

    Only the HOST registry counts. SEM has its own ``UnifiedDeviceRegistry``
    with a ``devices`` dict, and that one is ours to read: a name qualifies
    here only when this file binds it from ``device_registry.async_get(...)``,
    reads it straight off that call, or takes it as a parameter spelled one of
    :data:`_REGISTRY_PARAM_NAMES`.

    Covered shapes: plain assignment, the walrus, tuple unpacking, ``for`` and
    ``with`` targets, a rename chain, ``self._reg`` across methods, the inline
    ``dr.async_get(hass).devices`` chain, ``getattr(reg, "devices")``, and
    ``deleted_devices``. Known blind spots, so they are not mistaken for
    coverage: a registry fetched out of ``hass.data`` by hand, one passed as a
    parameter under some other name, and a file that names the
    ``device_registry`` module nowhere. All three are outside how this codebase
    gets a registry today.

    Returns ``[(relative_path, lineno, expression)]``.
    """
    hits = []
    for rel, tree in _production_files(root, skip_dirs):
        modules, getters = _device_registry_names(tree)
        if not modules and not getters:
            continue
        bound = _bound_registry_names(tree, modules, getters)
        params = _registry_parameters(tree)

        def _holds(node, lineno, _b=bound, _p=params,
                   _m=modules, _g=getters) -> bool:
            name = _dotted(node)
            if name in _b or _is_registry_call(node, _m, _g):
                return True
            return any(name == pname and lo <= lineno <= hi
                       for pname, lo, hi in _p)

        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and n.attr in _HOST_REGISTRY_MAPPINGS:
                if _holds(n.value, n.lineno):
                    hits.append((str(rel), n.lineno,
                                 f"{_dotted(n.value) or 'async_get(...)'}.{n.attr}"))
            elif isinstance(n, ast.Call) and _callee_name(n) == "getattr" \
                    and len(n.args) >= 2 \
                    and isinstance(n.args[1], ast.Constant) \
                    and n.args[1].value in _HOST_REGISTRY_MAPPINGS:
                if _holds(n.args[0], n.lineno):
                    hits.append((str(rel), n.lineno,
                                 f'getattr({_dotted(n.args[0])}, '
                                 f'"{n.args[1].value}")'))
    return sorted(set(hits))
