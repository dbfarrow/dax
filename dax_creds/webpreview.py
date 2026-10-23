"""Stored webpreview ports, and the env index page.

Each env's webpreview port is assigned once and stored in its ~/.dax.yaml
entry as `webpreview: {port: N}`. A hash of the env's folder path is only the
starting guess at assignment time; it moves up past ports other envs already
hold, so ports are unique across envs by construction. Ports are written in
exactly two places: the creation wizard (and restore, which registers a new
env the same way) and `dax env ports --assign`. See
docs/design/2026-10-23-webpreview-ports.md.

The index page is a local HTML file listing every env with a link to its
preview. The user bookmarks it once; dax rewrites it whenever the registry
changes or an env launches. Whether an env is running is checked by the page
itself, live, each time it is opened.
"""
import hashlib
import html
from pathlib import Path

DEFAULT_BASE_PORT = 8000
PORT_RANGE = 1000


def base_port(config):
    """The configured base (`defaults.webpreview_base_port`), or 8000."""
    defaults = config.get('defaults') if isinstance(config.get('defaults'), dict) else {}
    return int(defaults.get('webpreview_base_port') or DEFAULT_BASE_PORT)


def port_range(base):
    return range(base, base + PORT_RANGE)


def hashed_port(dir_path, base=DEFAULT_BASE_PORT):
    """The starting guess for a folder: deterministic, inside the range."""
    digest = int(hashlib.md5(str(dir_path).encode()).hexdigest(), 16)
    return base + (digest % PORT_RANGE)


def stored_port(entry):
    """An env entry's stored port, or None."""
    block = (entry or {}).get('webpreview')
    if isinstance(block, dict) and block.get('port'):
        return int(block['port'])
    return None


def _first_free(dir_path, base, taken):
    candidate = hashed_port(dir_path, base)
    for _ in range(PORT_RANGE):
        if candidate not in taken:
            return candidate
        candidate = base + ((candidate - base + 1) % PORT_RANGE)
    raise ValueError('every webpreview port from {} to {} is already assigned'.format(
        base, base + PORT_RANGE - 1))


def port_for_new_env(config, dir_path):
    """The port to store for an env being registered at `dir_path`: its
    hashed guess, moved up past every port another env already holds."""
    taken = {p for p in (stored_port(e) for e in (config.get('projects') or {}).values())
             if p is not None}
    return _first_free(dir_path, base_port(config), taken)


def plan_port_assignment(config):
    """[(env name, old port or None, new port), ...] for every env that
    `dax env ports --assign` would change, in registry order.

    An env keeps its port when it is inside the current range and no
    earlier env in the registry already holds it. Every other env (no port,
    a port outside the range after a base change, or a duplicate) gets its
    hashed guess, moved up past every port already kept or given out. With
    nothing changed since the last pass, the plan is empty.
    """
    base = base_port(config)
    valid = port_range(base)
    projects = config.get('projects') or {}

    taken, needs = set(), []
    for name, entry in projects.items():
        port = stored_port(entry)
        if port is not None and port in valid and port not in taken:
            taken.add(port)
        else:
            needs.append(name)

    changes = []
    for name in needs:
        entry = projects[name] or {}
        new = _first_free(entry.get('dir', name), base, taken)
        taken.add(new)
        changes.append((name, stored_port(entry), new))
    return changes


def set_port(entry, port):
    """Store `port` on an env entry, keeping any other webpreview keys."""
    block = entry.get('webpreview')
    if isinstance(block, dict):
        block['port'] = port
    else:
        entry['webpreview'] = {'port': port}


# --- the index page -----------------------------------------------------------

def index_path():
    return Path.home() / '.local' / 'state' / 'dax' / 'envs.html'


def _index_rows(config):
    """[(tenant, name, dir, port or None), ...] in `dax env list` order:
    tenants in the order the `tenants:` block lists them, then alphabetical,
    envs with no tenant last, names sorted within each. Hidden tenants are
    left out."""
    from dax_creds.config import hidden_tenants

    hidden = hidden_tenants(config)
    listed = config.get('tenants') if isinstance(config.get('tenants'), dict) else {}
    order = {t: i for i, t in enumerate(listed)}
    rows = []
    for name, entry in (config.get('projects') or {}).items():
        entry = entry or {}
        tenant = entry.get('tenant') or '-'
        if tenant in hidden:
            continue
        rows.append((tenant, name, entry.get('dir', ''), stored_port(entry)))
    rows.sort(key=lambda r: (r[0] == '-', order.get(r[0], len(order)), r[0], r[1]))
    return rows


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>dax envs</title>
<style>
:root {{ --bg: #ffffff; --fg: #1f2328; --muted: #656d76; --line: #d0d7de;
        --up: #1a7f37; --down: #8c959f; --link: #0969da; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #0d1117; --fg: #e6edf3; --muted: #8d96a0; --line: #30363d;
          --up: #3fb950; --down: #6e7681; --link: #4493f8; }}
}}
body {{ background: var(--bg); color: var(--fg); margin: 0 auto; padding: 24px 16px;
       max-width: 960px; font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI",
       Helvetica, Arial, sans-serif; }}
h1 {{ font-size: 20px; margin: 0 0 4px; }}
p.note {{ color: var(--muted); margin: 0 0 20px; }}
h2 {{ font-size: 14px; text-transform: uppercase; letter-spacing: .04em;
     color: var(--muted); margin: 24px 0 6px; }}
table {{ border-collapse: collapse; width: 100%; }}
td {{ border-top: 1px solid var(--line); padding: 6px 8px; vertical-align: top; }}
td.status {{ width: 6.5em; white-space: nowrap; color: var(--down); }}
td.status.up {{ color: var(--up); font-weight: 600; }}
td.name {{ width: 14em; font-weight: 600; }}
td.dir {{ color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
         font-size: 12px; word-break: break-all; }}
a {{ color: var(--link); }}
</style>
</head>
<body>
<h1>dax envs</h1>
<p class="note">Written by dax {written}. Running is checked when this page is opened.</p>
{groups}
<script>
document.querySelectorAll('tr[data-url]').forEach(function (row) {{
  var cell = row.querySelector('td.status');
  var ctl = new AbortController();
  var timer = setTimeout(function () {{ ctl.abort(); }}, 1500);
  fetch(row.dataset.url, {{ mode: 'no-cors', cache: 'no-store', signal: ctl.signal }})
    .then(function () {{ cell.textContent = '\\u25cf running'; cell.classList.add('up'); }})
    .catch(function () {{ cell.textContent = 'stopped'; }})
    .finally(function () {{ clearTimeout(timer); }});
}});
</script>
</body>
</html>
"""


def render_index(config, written=''):
    groups, current, rows_html = [], None, []

    def _flush():
        if current is not None:
            groups.append('<h2>{}</h2>\n<table>\n{}\n</table>'.format(
                html.escape(current), '\n'.join(rows_html)))

    for tenant, name, dir_path, port in _index_rows(config):
        if tenant != current:
            _flush()
            current, rows_html = tenant, []
        if port is None:
            row = ('<tr><td class="status">no port</td><td class="name">{}</td>'
                   '<td>run <code>dax env ports --assign</code></td>'
                   '<td class="dir">{}</td></tr>').format(html.escape(name), html.escape(dir_path))
        else:
            url = 'http://localhost:{}/'.format(port)
            row = ('<tr data-url="{url}"><td class="status">checking</td>'
                   '<td class="name">{name}</td><td><a href="{url}">{url}</a></td>'
                   '<td class="dir">{dir}</td></tr>').format(
                       url=url, name=html.escape(name), dir=html.escape(dir_path))
        rows_html.append(row)
    _flush()
    if not groups:
        groups.append('<p class="note">No envs registered.</p>')
    return _PAGE.format(written=html.escape(written), groups='\n'.join(groups))


def write_index(config, written=''):
    """Write the index page and return its path."""
    path = index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_index(config, written))
    return path
