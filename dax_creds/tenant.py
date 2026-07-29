"""Resolve which tenant (customer) a directory belongs to.

Implements the design doc's resolution table
(docs/design/2026-07-27-tenant-isolation.md, decisions 7-9), revised
2026-07-28 to drop `unattributed` as an automatic fallback:

    single-tenant repo, at the root, declared     -> starts, state -> its tenant
    single-tenant repo, at the root, undeclared   -> refuse
    single-tenant repo, in a subdirectory         -> refuse (root is the sandbox)
    multi-tenant repo, at the root, declared      -> starts, state -> its tenant
    multi-tenant repo, at the root, undeclared    -> refuse
    multi-tenant repo, mapped subdir              -> starts, state -> that tenant
    multi-tenant repo, tenant_subdir base itself  -> refuse (no single answer)
    multi-tenant repo, unmapped subdir            -> refuse

(A row for project creds spanning multiple tenant labels is a separate,
deferred check; see CLAUDE.md Backlog. Not implemented here.)

There is no silent `unattributed/<reponame>` fallback anywhere in this table
- an undeclared directory always refuses rather than pooling into a default
tenant. That is a deliberate choice, not an oversight: cleaning up a tenant
assigned after the fact is real, avoidable friction, so `dax run` (Gate A,
`_ensure_tenants_classified` in `dax.py`) interactively leads the user
through declaring anything undeclared *before* a container ever launches -
by the time this module is asked to resolve anything, it should already be
declared. Refusing here instead of falling back is the safety net for
whatever reaches this module without having gone through that step (a
brand-new subdirectory created mid-session, or a manual/bypassed invocation).

Both single- and multi-tenant repos now let claude start **at the repo root
itself**, treated as its own project - working on discernment's own tooling
(its `CLAUDE.md`, skills library, bootstrap scripts) is not the same as
working a specific customer engagement under it, and needs its own tenant
and its own isolated state, distinct from any of the engagements below it.
This replaced an earlier, since-revised rule where a multi-tenant repo's
root always refused - discovered too narrow once real use turned up a
genuine need to work on the repo independent of any one engagement.

Whether a repo is multi-tenant is never inferred from what `.dax-tenant`
files happen to exist - it is an explicit `multi_tenant` flag every caller
must supply (sourced from `projects.<name>.multi_tenant` in `~/.dax.yaml`,
defaulting to False when unset). That flag draws a hard line between where
claude may even be started, not just where a tenant may be declared:

    multi_tenant=False   claude may start at the repo root (only) - that IS
                          the sandbox. A child directory refuses outright,
                          even if it happens to carry its own .dax-tenant
                          file.
    multi_tenant=True     claude may start at the repo root (its own
                          project, per above) or at an immediate child of
                          `tenant_subdir` (or the root, if `tenant_subdir`
                          isn't set) that carries its own .dax-tenant file.
                          The `tenant_subdir` base itself (e.g. `processes/`)
                          never resolves - there is no single answer for
                          "all engagements collectively".

Claude may only ever be started at a project's mount root or exactly one
directory below it - never deeper - as an outer bound ahead of either branch
above.

Two entry points, sharing the same logic, so Gate A and Gate B can never
disagree about what a repo resolves to:

    all_tenant_projects(repo_root, multi_tenant)   Gate A (`dax run`, host):
                                      every tenant/project pair the repo
                                      could resolve to, for mount-scoping.
    resolve_for_cwd(cwd, home, multi_tenant)   Gate B (`claude` wrapper,
                                      in-container): resolve one cwd,
                                      self-determining the repo root from
                                      decision 2's mount convention (a
                                      project always mounts as a single
                                      directory directly under $HOME).
"""
from pathlib import Path

from dax_creds.config import dir_basename

TENANT_FILE = '.dax-tenant'

# Never treated as a candidate tenant subdirectory, even if it happens to
# contain a .dax-tenant file - these are tooling/vendor directories, not
# project subdirectories a user would `cd` into and start claude from.
_NOT_A_PROJECT_SUBDIR = {
    '.git', 'node_modules', '__pycache__', '.venv', 'venv-2.7', 'venv-3',
    '.pytest_cache',
}


class Resolution:
    """One resolution-table outcome.

    tenant: resolved tenant label, or None if refused.
    project: basename of the directory whose .dax-tenant governs this
        resolution (the repo root, whether single- or multi-tenant; or the
        matched immediate child in multi-tenant mode) - the <project>
        segment of ~/.local/state/dax/tenants/<tenant>/<project>/.
    refuse: True when there is no safe resolution - too deep, undeclared
        (root or subdirectory, either mode), the tenant_subdir base itself,
        or outside any mounted project. There is no automatic fallback
        anywhere in this table; see the module docstring for why.
    detail: human-readable explanation, for wrapper/CLI messages.
    """

    def __init__(self, tenant, project, refuse, detail):
        self.tenant = tenant
        self.project = project
        self.refuse = refuse
        self.detail = detail

    def __repr__(self):
        return (
            'Resolution(tenant={!r}, project={!r}, refuse={!r}, '
            'detail={!r})'.format(
                self.tenant, self.project, self.refuse, self.detail)
        )

    def __eq__(self, other):
        return isinstance(other, Resolution) and vars(self) == vars(other)


def _read_tenant_label(path):
    """Tenant name declared in a .dax-tenant file.

    None if the file is absent, empty, or whitespace-only - an accidental
    empty file is treated as "not declared" rather than minting a tenant
    literally named "".
    """
    try:
        text = path.read_text()
    except (FileNotFoundError, IsADirectoryError):
        return None
    return text.strip() or None


def _too_deep(cwd, repo_root):
    return cwd != repo_root and cwd.parent != repo_root


def _too_deep_resolution(cwd, repo_root):
    return Resolution(
        tenant=None, project=None, refuse=True,
        detail=(
            '{} is more than one level below the mounted project root {}; '
            'start claude at the project root or in a direct subdirectory '
            'of it, not deeper'.format(cwd, repo_root)))


def _undeclared_refusal(subdir, reponame):
    return Resolution(
        tenant=None, project=None, refuse=True,
        detail='{} has no {}; run `dax tenant set {} <tenant>` (or `dax tenant '
               'classify` to be led through it) before starting claude '
               'here'.format(subdir, TENANT_FILE, subdir))


def resolve_tenant(cwd, repo_root, multi_tenant, tenant_subdir=''):
    """Apply the resolution table for `cwd` within a repo rooted at `repo_root`.

    `tenant_subdir` (multi-tenant only): some repos keep their labeled
    directories one level further in than the root - discernment's actual
    tenant subdirectories sit under `processes/`, not directly under the
    repo root, and restructuring the repo or splitting it into two projects
    both cost more than this is worth for what is, today, a single project
    with this shape. When set, labeled subdirectories are looked for under
    `repo_root/tenant_subdir` instead of `repo_root` directly; nothing else
    about the resolution table changes. Not generalized to more than one
    fixed subdirectory name - no second project needs that yet.
    """
    cwd = Path(cwd)
    repo_root = Path(repo_root)
    reponame = dir_basename(repo_root)

    if not multi_tenant:
        if _too_deep(cwd, repo_root):
            return _too_deep_resolution(cwd, repo_root)
        if cwd != repo_root:
            return Resolution(
                tenant=None, project=None, refuse=True,
                detail='{} is a single-tenant project; claude may only start '
                       'at the project root {}, not a subdirectory'.format(
                           reponame, repo_root))
        tenant = _read_tenant_label(repo_root / TENANT_FILE)
        if tenant is None:
            return _undeclared_refusal(repo_root, reponame)
        return Resolution(
            tenant=tenant, project=reponame, refuse=False,
            detail="single tenant '{}' declared at the root of {}".format(tenant, reponame))

    # multi_tenant=True. The repo root is its own project (decided 2026-07-28:
    # working on the repo itself - its tooling, skills, bootstrap scripts -
    # isn't the same as working a specific engagement under it, and deserves
    # its own tenant and isolated state). The tenant_subdir base (e.g.
    # processes/ itself, when it differs from the root) never resolves -
    # there is no single answer for "all engagements collectively" - and
    # only its own immediate children are ever consulted beyond that.
    if cwd == repo_root:
        tenant = _read_tenant_label(repo_root / TENANT_FILE)
        if tenant is None:
            return _undeclared_refusal(repo_root, reponame)
        return Resolution(
            tenant=tenant, project=reponame, refuse=False,
            detail="tenant '{}' declared at the root of {}".format(tenant, reponame))

    tenant_base = (repo_root / tenant_subdir) if tenant_subdir else repo_root

    if cwd == tenant_base:
        if tenant_subdir:
            detail = ('{} is a multi-tenant project; start claude in a labeled '
                       'subdirectory under {}, not {} itself'.format(
                           reponame, tenant_base, tenant_base))
        else:
            detail = ('{} is a multi-tenant project; start claude in a labeled '
                       'subdirectory, not the repo root'.format(reponame))
        return Resolution(tenant=None, project=None, refuse=True, detail=detail)

    if cwd.parent != tenant_base:
        return Resolution(
            tenant=None, project=None, refuse=True,
            detail='{} is not a direct subdirectory of {}; claude may only '
                   'start in a labeled subdirectory there'.format(cwd, tenant_base))

    tenant = _read_tenant_label(cwd / TENANT_FILE)
    if tenant is None:
        return _undeclared_refusal(cwd, reponame)
    return Resolution(
        tenant=tenant, project=dir_basename(cwd), refuse=False,
        detail='resolved via {}/{}'.format(cwd, TENANT_FILE))


def all_tenant_projects(repo_root, multi_tenant, tenant_subdir=''):
    """Every (tenant, project) pair this repo could resolve to.

    Gate A's (`dax run`) mount-scoping question: which
    tenants/<tenant>/<project>/ subtrees must be mounted so that wherever a
    user `cd`s to during the session (root or one level down - see module
    docstring), Gate B's resolution has already been made available.
    `tenant_subdir` mirrors `resolve_tenant`'s parameter of the same name.

    Returns an empty set for anything undeclared - by the time this runs
    (Gate A, before a container's mounts are computed), `dax run` should
    already have led the user through declaring everything relevant via
    `_ensure_tenants_classified`; an empty result here means that step was
    skipped or bypassed, not that pooling into some default is the fallback.
    """
    repo_root = Path(repo_root)
    reponame = dir_basename(repo_root)
    root_tenant = _read_tenant_label(repo_root / TENANT_FILE)

    if not multi_tenant:
        if root_tenant is None:
            return set()
        return {(root_tenant, reponame)}

    pairs = set()
    if root_tenant:
        pairs.add((root_tenant, reponame))

    tenant_base = (repo_root / tenant_subdir) if tenant_subdir else repo_root
    if tenant_base.is_dir():
        for child in tenant_base.iterdir():
            if not child.is_dir() or child.name in _NOT_A_PROJECT_SUBDIR:
                continue
            tenant = _read_tenant_label(child / TENANT_FILE)
            if tenant:
                pairs.add((tenant, dir_basename(child)))
    return pairs


def resolve_for_cwd(cwd, home, multi_tenant, project_name, tenant_subdir=''):
    """Gate B entry point: resolve a tenant from a cwd inside a container.

    `home` is the container's $HOME. `project_name` is the mount name Gate A
    (`dax run`) actually used for this container's project (its
    `workdir_name`, decision 2) - handed down explicitly, not inferred from
    whatever cwd's top-level directory happens to be. That distinction
    matters: `$HOME` legitimately has other directory-level mounts sitting
    right alongside a project's own (`~/.claude`, `~/.augment`, `~/.aws`).
    Without checking `project_name`, a user who `cd`s into one of those and
    runs `claude` there would have it treated as if it were the project root
    - undeclared, so it would refuse for the wrong reason instead of the
    right one. Checking the expected name first closes that off before any
    `.dax-tenant` lookup happens at all.
    `tenant_subdir` mirrors `resolve_tenant`'s parameter of the same name.
    """
    cwd = Path(cwd).resolve()
    home = Path(home).resolve()
    try:
        rel = cwd.relative_to(home)
    except ValueError:
        return Resolution(
            tenant=None, project=None, refuse=True,
            detail='{} is not inside {}'.format(cwd, home))
    if not rel.parts:
        return Resolution(
            tenant=None, project=None, refuse=True,
            detail='{} is $HOME itself, not inside a mounted project'.format(cwd))
    if rel.parts[0] != project_name:
        return Resolution(
            tenant=None, project=None, refuse=True,
            detail='{} is not inside the mounted project {}/{}'.format(
                cwd, home, project_name))

    repo_root = home / project_name
    return resolve_tenant(cwd, repo_root, multi_tenant, tenant_subdir)
