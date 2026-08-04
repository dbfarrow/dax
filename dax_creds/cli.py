import os
import sys
from dax_creds.client import get_token, open_url, list_credentials, CredentialNotAvailable
from dax_creds.tenant import resolve_for_cwd


def main():
    if len(sys.argv) < 2:
        print('Usage: dax-creds <get|open-url|list|resolve-tenant> ...', file=sys.stderr)
        sys.exit(1)

    subcmd = sys.argv[1]

    if subcmd == 'list':
        if len(sys.argv) != 2:
            print('Usage: dax-creds list', file=sys.stderr)
            sys.exit(1)
        try:
            creds = list_credentials()
        except CredentialNotAvailable as e:
            print(f'dax-creds: {e}', file=sys.stderr)
            sys.exit(1)
        if not creds:
            return
        width = max(len(c['name']) for c in creds)
        for c in creds:
            print('{:<{w}}  {}'.format(c['name'], c.get('provider') or '', w=width))

    elif subcmd == 'get':
        if len(sys.argv) != 3:
            print('Usage: dax-creds get <credential>', file=sys.stderr)
            sys.exit(1)
        try:
            token = get_token(sys.argv[2])
            print(token, end='')
        except CredentialNotAvailable as e:
            print(f'dax-creds: {e}', file=sys.stderr)
            sys.exit(1)

    elif subcmd == 'open-url':
        if len(sys.argv) != 3:
            print('Usage: dax-creds open-url <url>', file=sys.stderr)
            sys.exit(1)
        cred_name = os.environ.get('DAX_CREDS_LOGIN_CRED', '')
        if not cred_name:
            print('dax-creds: DAX_CREDS_LOGIN_CRED is not set', file=sys.stderr)
            sys.exit(1)
        try:
            open_url(cred_name, sys.argv[2])
        except CredentialNotAvailable as e:
            print(f'dax-creds: {e}', file=sys.stderr)
            sys.exit(1)

    elif subcmd == 'resolve-tenant':
        if len(sys.argv) != 2:
            print('Usage: dax-creds resolve-tenant', file=sys.stderr)
            sys.exit(1)
        project_name = os.environ.get('DAX_PROJECT_NAME')
        if not project_name:
            print('dax-creds: DAX_PROJECT_NAME is not set - dax run should '
                  'always set this alongside DAX_TENANT_STATE', file=sys.stderr)
            sys.exit(1)
        multi_tenant = bool(os.environ.get('DAX_MULTI_TENANT'))
        tenant_subdir = os.environ.get('DAX_TENANT_SUBDIR', '')
        result = resolve_for_cwd(
            os.getcwd(), os.environ.get('HOME', ''), multi_tenant, project_name,
            tenant_subdir)
        if result.refuse:
            print(f'dax-creds: {result.detail}', file=sys.stderr)
            sys.exit(1)
        print(f'TENANT={result.tenant}')
        print(f'PROJECT={result.project}')

    else:
        print(f'dax-creds: unknown subcommand {subcmd!r}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
