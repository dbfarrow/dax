import os
import sys
from dax_creds.client import get_token, open_url, list_credentials, CredentialNotAvailable


def main():
    if len(sys.argv) < 2:
        print('Usage: dax-creds <get|open-url|list> ...', file=sys.stderr)
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

    else:
        print(f'dax-creds: unknown subcommand {subcmd!r}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
