from dax_creds.tenant import (
    Resolution,
    resolve_tenant,
    all_tenant_projects,
    resolve_for_cwd,
    TENANT_FILE,
)


def _declare(path, tenant):
    path.mkdir(parents=True, exist_ok=True)
    (path / TENANT_FILE).write_text(tenant)


# --- depth boundary: claude only ever starts at the root or one level down -

def test_resolve_tenant_refuses_more_than_one_level_deep_single_tenant(tmp_path):
    repo = tmp_path / 'discernment'
    _declare(repo, 'personal')  # would otherwise resolve fine
    deep = repo / 'src' / 'nested'
    deep.mkdir(parents=True)

    result = resolve_tenant(deep, repo, multi_tenant=False)

    assert result.refuse
    assert result.tenant is None
    assert 'more than one level below' in result.detail


def test_resolve_tenant_refuses_more_than_one_level_deep_multi_tenant(tmp_path):
    repo = tmp_path / 'discernment'
    repo.mkdir()
    deep = repo / 'project_a' / 'nested'
    _declare(deep, 'ysecurity')  # even a directly-labeled dir, if too deep, refuses

    result = resolve_tenant(deep, repo, multi_tenant=True)

    assert result.refuse


def test_resolve_tenant_refuses_when_cwd_unrelated_to_repo_root(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    unrelated = tmp_path / 'somewhere-else'
    unrelated.mkdir()

    result = resolve_tenant(unrelated, repo, multi_tenant=False)

    assert result.refuse


# --- single-tenant mode: tenant always comes from the root, only the root --

def test_single_tenant_resolves_at_root(tmp_path):
    repo = tmp_path / 'fabric'
    _declare(repo, 'personal')

    result = resolve_tenant(repo, repo, multi_tenant=False)

    assert result == Resolution(tenant='personal', project='fabric',
                                 refuse=False, detail=result.detail)


def test_single_tenant_refuses_at_immediate_child(tmp_path):
    # The root is the sandbox - a single-tenant project has no legal cwd
    # other than the root itself, even one level down.
    repo = tmp_path / 'fabric'
    _declare(repo, 'personal')
    subdir = repo / 'src'
    subdir.mkdir()

    result = resolve_tenant(subdir, repo, multi_tenant=False)

    assert result.refuse
    assert result.tenant is None
    assert 'project root' in result.detail


def test_single_tenant_refuses_at_child_even_with_its_own_tenant_file(tmp_path):
    # A child's own .dax-tenant grants nothing in this mode - only the root
    # is ever a legal place to start, regardless of what files exist below it.
    repo = tmp_path / 'fabric'
    _declare(repo, 'personal')
    subdir = repo / 'src'
    _declare(subdir, 'someone-else')

    result = resolve_tenant(subdir, repo, multi_tenant=False)

    assert result.refuse


def test_single_tenant_undeclared_at_root_refuses(tmp_path):
    # No automatic unattributed/<reponame> fallback (dropped 2026-07-28):
    # an undeclared root refuses, with a hint at how to declare it, rather
    # than silently pooling into a default tenant.
    repo = tmp_path / 'iandidit'
    repo.mkdir()

    result = resolve_tenant(repo, repo, multi_tenant=False)

    assert result.refuse
    assert result.tenant is None
    assert 'dax tenant set' in result.detail


def test_single_tenant_refuses_at_child_even_when_root_undeclared(tmp_path):
    # cwd-is-root-only is checked before the undeclared-root refusal - an
    # undeclared root doesn't loosen where claude may start.
    repo = tmp_path / 'iandidit'
    subdir = repo / 'src'
    subdir.mkdir(parents=True)

    result = resolve_tenant(subdir, repo, multi_tenant=False)

    assert result.refuse


def test_single_tenant_empty_root_file_counts_as_undeclared(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / TENANT_FILE).write_text('   \n')

    result = resolve_tenant(repo, repo, multi_tenant=False)

    assert result.refuse
    assert result.tenant is None


# --- multi-tenant mode: tenant always comes from an immediate child -------

def test_multi_tenant_undeclared_root_refuses(tmp_path):
    # Root is now its own project even in multi-tenant mode (2026-07-28) -
    # but still refuses like anywhere else in this table when undeclared,
    # rather than falling back to unattributed or the old "root never
    # resolves" rule.
    repo = tmp_path / 'discernment'
    _declare(repo / 'project_ysecurity_comp_model', 'ysecurity')

    result = resolve_tenant(repo, repo, multi_tenant=True)

    assert result.refuse
    assert result.tenant is None
    assert 'dax tenant set' in result.detail


def test_multi_tenant_root_resolves_when_declared(tmp_path):
    # The repo root is its own project, distinct from any customer
    # engagement under it - working on discernment's own tooling needs its
    # own tenant and its own isolated state.
    repo = tmp_path / 'discernment'
    _declare(repo, 'personal')
    _declare(repo / 'project_ysecurity_comp_model', 'ysecurity')

    result = resolve_tenant(repo, repo, multi_tenant=True)

    assert not result.refuse
    assert result.tenant == 'personal'
    assert result.project == 'discernment'


def test_multi_tenant_resolves_mapped_child(tmp_path):
    repo = tmp_path / 'discernment'
    sub_a = repo / 'project_ysecurity_comp_model'
    _declare(sub_a, 'ysecurity')
    _declare(repo / 'project_augment', 'augment')

    result = resolve_tenant(sub_a, repo, multi_tenant=True)

    assert result.tenant == 'ysecurity'
    assert result.project == 'project_ysecurity_comp_model'
    assert not result.refuse


def test_multi_tenant_refuses_unmapped_child(tmp_path):
    repo = tmp_path / 'discernment'
    _declare(repo / 'project_ysecurity_comp_model', 'ysecurity')
    unmapped = repo / 'project_someone_new'
    unmapped.mkdir(parents=True)

    result = resolve_tenant(unmapped, repo, multi_tenant=True)

    assert result.refuse
    assert str(unmapped) in result.detail


def test_multi_tenant_has_no_unattributed_fallback(tmp_path):
    # Fail toward over-isolation: an explicitly flagged multi-tenant repo
    # never silently pools into unattributed/, even with nothing labeled yet.
    repo = tmp_path / 'discernment'
    child = repo / 'project_new'
    child.mkdir(parents=True)

    result = resolve_tenant(child, repo, multi_tenant=True)

    assert result.refuse
    assert result.tenant != 'unattributed'


# --- multi-tenant with tenant_subdir: labeled dirs one level further in ----
#
# discernment's actual tenant subdirectories sit under processes/, not
# directly under the repo root - this is the one-off accommodation for that,
# not a generalized arbitrary-depth mechanism.

def test_multi_tenant_with_subdir_resolves_mapped_child(tmp_path):
    repo = tmp_path / 'discernment'
    _declare(repo / 'processes' / 'project_ysecurity_comp_model', 'ysecurity')

    result = resolve_tenant(repo / 'processes' / 'project_ysecurity_comp_model',
                             repo, multi_tenant=True, tenant_subdir='processes')

    assert result.tenant == 'ysecurity'
    assert result.project == 'project_ysecurity_comp_model'
    assert not result.refuse


def test_multi_tenant_with_subdir_refuses_at_true_repo_root_when_undeclared(tmp_path):
    repo = tmp_path / 'discernment'
    _declare(repo / 'processes' / 'project_ysecurity_comp_model', 'ysecurity')

    result = resolve_tenant(repo, repo, multi_tenant=True, tenant_subdir='processes')

    assert result.refuse


def test_multi_tenant_with_subdir_root_resolves_when_declared(tmp_path):
    # The root-is-its-own-project rule applies regardless of whether
    # tenant_subdir is set - discernment's own tooling work still needs its
    # own tenant even though its engagements live under processes/.
    repo = tmp_path / 'discernment'
    _declare(repo, 'personal')
    _declare(repo / 'processes' / 'project_ysecurity_comp_model', 'ysecurity')

    result = resolve_tenant(repo, repo, multi_tenant=True, tenant_subdir='processes')

    assert not result.refuse
    assert result.tenant == 'personal'
    assert result.project == 'discernment'


def test_multi_tenant_with_subdir_refuses_at_the_subdir_itself(tmp_path):
    repo = tmp_path / 'discernment'
    _declare(repo / 'processes' / 'project_ysecurity_comp_model', 'ysecurity')

    result = resolve_tenant(repo / 'processes', repo, multi_tenant=True, tenant_subdir='processes')

    assert result.refuse


def test_multi_tenant_with_subdir_refuses_sibling_top_level_dir(tmp_path):
    # A directory that exists at the repo root but isn't the configured
    # tenant_subdir - e.g. discernment's other top-level content - must
    # still refuse, not be treated as if it were under processes/.
    repo = tmp_path / 'discernment'
    _declare(repo / 'processes' / 'project_ysecurity_comp_model', 'ysecurity')
    (repo / 'docs').mkdir(parents=True)

    result = resolve_tenant(repo / 'docs', repo, multi_tenant=True, tenant_subdir='processes')

    assert result.refuse


def test_multi_tenant_with_subdir_refuses_unmapped_child(tmp_path):
    repo = tmp_path / 'discernment'
    _declare(repo / 'processes' / 'project_ysecurity_comp_model', 'ysecurity')
    unmapped = repo / 'processes' / 'project_someone_new'
    unmapped.mkdir(parents=True)

    result = resolve_tenant(unmapped, repo, multi_tenant=True, tenant_subdir='processes')

    assert result.refuse
    assert str(unmapped) in result.detail


def test_all_tenant_projects_multi_tenant_with_subdir(tmp_path):
    repo = tmp_path / 'discernment'
    _declare(repo / 'processes' / 'project_ysecurity_comp_model', 'ysecurity')
    _declare(repo / 'processes' / 'project_augment', 'augment')
    _declare(repo / 'project_at_root_should_be_ignored', 'should-not-count')

    pairs = all_tenant_projects(repo, multi_tenant=True, tenant_subdir='processes')

    assert pairs == {
        ('ysecurity', 'project_ysecurity_comp_model'),
        ('augment', 'project_augment'),
    }


def test_resolve_for_cwd_multi_tenant_with_subdir_mapped_child(tmp_path):
    home = tmp_path / 'home'
    repo = home / 'discernment'
    _declare(repo / 'processes' / 'project_ysecurity_comp_model', 'ysecurity')

    result = resolve_for_cwd(repo / 'processes' / 'project_ysecurity_comp_model', home,
                              multi_tenant=True, project_name='discernment',
                              tenant_subdir='processes')

    assert result.tenant == 'ysecurity'
    assert result.project == 'project_ysecurity_comp_model'


def test_resolve_for_cwd_multi_tenant_with_subdir_refuses_at_root(tmp_path):
    home = tmp_path / 'home'
    repo = home / 'discernment'
    _declare(repo / 'processes' / 'project_ysecurity_comp_model', 'ysecurity')

    result = resolve_for_cwd(repo, home, multi_tenant=True, project_name='discernment',
                              tenant_subdir='processes')

    assert result.refuse


# --- all_tenant_projects: Gate A's mount-scoping question -------------------

def test_all_tenant_projects_single_tenant_declared(tmp_path):
    repo = tmp_path / 'fabric'
    _declare(repo, 'personal')

    assert all_tenant_projects(repo, multi_tenant=False) == {('personal', 'fabric')}


def test_all_tenant_projects_single_tenant_undeclared(tmp_path):
    # No mount to scope for an undeclared project - dax run's classification
    # step (Gate A) should always resolve this before mounts are computed;
    # an empty result here means that step was skipped, not a default to
    # fall back to.
    repo = tmp_path / 'fabric'
    repo.mkdir()

    assert all_tenant_projects(repo, multi_tenant=False) == set()


def test_all_tenant_projects_multi_tenant(tmp_path):
    repo = tmp_path / 'discernment'
    _declare(repo / 'project_ysecurity_comp_model', 'ysecurity')
    _declare(repo / 'project_augment', 'augment')

    pairs = all_tenant_projects(repo, multi_tenant=True)

    assert pairs == {
        ('ysecurity', 'project_ysecurity_comp_model'),
        ('augment', 'project_augment'),
    }


def test_all_tenant_projects_multi_tenant_includes_declared_root(tmp_path):
    # Gate A must mount the root's own state tree too, alongside every
    # labeled engagement, now that the root resolves as its own project.
    repo = tmp_path / 'discernment'
    _declare(repo, 'personal')
    _declare(repo / 'project_ysecurity_comp_model', 'ysecurity')

    pairs = all_tenant_projects(repo, multi_tenant=True)

    assert pairs == {
        ('personal', 'discernment'),
        ('ysecurity', 'project_ysecurity_comp_model'),
    }


def test_all_tenant_projects_multi_tenant_skips_git_and_node_modules(tmp_path):
    repo = tmp_path / 'discernment'
    _declare(repo / '.git', 'should-not-count')
    _declare(repo / 'node_modules', 'should-not-count-either')
    _declare(repo / 'project_ysecurity_comp_model', 'ysecurity')

    assert all_tenant_projects(repo, multi_tenant=True) == {
        ('ysecurity', 'project_ysecurity_comp_model')}


def test_all_tenant_projects_multi_tenant_empty_when_nothing_labeled(tmp_path):
    repo = tmp_path / 'discernment'
    repo.mkdir()
    (repo / 'project_new').mkdir()

    assert all_tenant_projects(repo, multi_tenant=True) == set()


# --- resolve_for_cwd: Gate B's entry point ----------------------------------

def test_resolve_for_cwd_single_tenant_at_root(tmp_path):
    home = tmp_path / 'home'
    repo = home / 'fabric'
    _declare(repo, 'personal')

    result = resolve_for_cwd(repo, home, multi_tenant=False, project_name='fabric')

    assert result.tenant == 'personal'
    assert result.project == 'fabric'


def test_resolve_for_cwd_single_tenant_refuses_at_immediate_child(tmp_path):
    home = tmp_path / 'home'
    repo = home / 'fabric'
    _declare(repo, 'personal')
    subdir = repo / 'src'
    subdir.mkdir(parents=True)

    result = resolve_for_cwd(subdir, home, multi_tenant=False, project_name='fabric')

    assert result.refuse


def test_resolve_for_cwd_multi_tenant_mapped_child(tmp_path):
    home = tmp_path / 'home'
    repo = home / 'discernment'
    _declare(repo / 'project_ysecurity_comp_model', 'ysecurity')
    _declare(repo / 'project_augment', 'augment')

    result = resolve_for_cwd(repo / 'project_ysecurity_comp_model', home,
                              multi_tenant=True, project_name='discernment')

    assert result.tenant == 'ysecurity'
    assert result.project == 'project_ysecurity_comp_model'


def test_resolve_for_cwd_multi_tenant_refuses_at_repo_root(tmp_path):
    home = tmp_path / 'home'
    repo = home / 'discernment'
    _declare(repo / 'project_ysecurity_comp_model', 'ysecurity')

    result = resolve_for_cwd(repo, home, multi_tenant=True, project_name='discernment')

    assert result.refuse


def test_resolve_for_cwd_refuses_when_cwd_is_home_itself(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()

    result = resolve_for_cwd(home, home, multi_tenant=False, project_name='fabric')

    assert result.refuse
    assert result.tenant is None


def test_resolve_for_cwd_refuses_when_cwd_outside_home(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    outside = tmp_path / 'not-home' / 'fabric'
    outside.mkdir(parents=True)

    result = resolve_for_cwd(outside, home, multi_tenant=False, project_name='fabric')

    assert result.refuse


def test_resolve_for_cwd_refuses_more_than_one_level_deep(tmp_path):
    # Two levels deep refuses via the outer depth bound - a stricter check
    # than the single-tenant root-only rule, but both land on refuse here.
    home = tmp_path / 'home'
    repo = home / 'fabric'
    _declare(repo, 'personal')
    deep = repo / 'src' / 'nested'
    deep.mkdir(parents=True)

    result = resolve_for_cwd(deep, home, multi_tenant=False, project_name='fabric')

    assert result.refuse


# --- resolve_for_cwd: cwd under an unrelated $HOME-level mount --------------
#
# $HOME legitimately has other directory-level mounts sitting right alongside
# a project's own (~/.claude, ~/.augment, ~/.aws - see .dax.yaml.example).
# Without checking the expected project_name, a user who `cd`s into one of
# those and runs claude there would have it treated as if it *were* the
# project root - undeclared there, so it would silently resolve to
# unattributed/.claude instead of refusing. These pin that it refuses.

def test_resolve_for_cwd_refuses_in_unrelated_home_level_mount(tmp_path):
    home = tmp_path / 'home'
    repo = home / 'fabric'
    _declare(repo, 'personal')
    other_mount = home / '.claude'  # e.g. claudedir - not a project at all
    other_mount.mkdir(parents=True)

    result = resolve_for_cwd(other_mount, home, multi_tenant=False, project_name='fabric')

    assert result.refuse
    assert result.tenant != 'unattributed'


def test_resolve_for_cwd_refuses_in_unrelated_mount_even_if_it_has_a_tenant_file(tmp_path):
    # Not just the undeclared case - even a stray .dax-tenant file sitting in
    # an unrelated mount (leftover, mistake, or a prior project's artifact)
    # must not be treated as this container's resolution.
    home = tmp_path / 'home'
    repo = home / 'fabric'
    _declare(repo, 'personal')
    other_mount = home / '.aws'
    _declare(other_mount, 'someone-else')

    result = resolve_for_cwd(other_mount, home, multi_tenant=False, project_name='fabric')

    assert result.refuse
    assert result.tenant != 'someone-else'


def test_resolve_for_cwd_refuses_when_top_level_dir_does_not_exist(tmp_path):
    # The project_name mismatch check applies even when nothing has been
    # mounted at that path at all - no crash, just a clean refuse.
    home = tmp_path / 'home'
    home.mkdir()

    result = resolve_for_cwd(home / 'nonexistent', home, multi_tenant=False,
                              project_name='fabric')

    assert result.refuse
    assert 'not inside the mounted project' in result.detail
