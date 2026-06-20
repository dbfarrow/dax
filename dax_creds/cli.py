import sys
from dax_creds.client import get_token, CredentialNotAvailable


def main():
    if len(sys.argv) != 3 or sys.argv[1] != 'get':
        print('Usage: dax-creds get <credential>', file=sys.stderr)
        sys.exit(1)
    credential_name = sys.argv[2]
    try:
        token = get_token(credential_name)
        print(token, end='')
    except CredentialNotAvailable as e:
        print(f'dax-creds: {e}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
