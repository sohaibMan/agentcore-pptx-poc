"""Configure a role for the fixed sandbox account after AWS SSO login."""
import configparser
import hashlib
import json
import os
from pathlib import Path

import boto3
from botocore import UNSIGNED
from botocore.config import Config

ACCOUNT = os.environ['AWS_ACCOUNT_ID']
PROFILE = os.environ['AWS_PROFILE']


def sso_client(region):
    return boto3.client('sso', region_name=region,
                        config=Config(signature_version=UNSIGNED,
                                      connect_timeout=10, read_timeout=10,
                                      retries={'max_attempts': 1}))


def select_role(sso, token, current=None):
    roles = sorted({role['roleName']
                    for page in sso.get_paginator('list_account_roles').paginate(
                        accessToken=token, accountId=ACCOUNT)
                    for role in page['roleList']})
    if not roles:
        raise RuntimeError(f'No SSO roles are assigned to you in account {ACCOUNT}. '
                           'Ask your AWS administrator for access to this account.')
    if current in roles:
        return current
    print(f'Available roles in sandbox account {ACCOUNT}:')
    for number, role in enumerate(roles, 1):
        print(f'  {number}. {role}')
    if len(roles) == 1:
        return roles[0]
    while True:
        choice = input('Select role number: ').strip()
        if choice.isdigit() and 1 <= int(choice) <= len(roles):
            return roles[int(choice) - 1]
        print('Enter one of the role numbers above.')


def main():
    path = Path(os.environ['AWS_CONFIG_FILE'])
    config = configparser.RawConfigParser()
    config.read(path)
    profile = config[f'profile {PROFILE}']
    session = profile['sso_session']
    # AWS CLI caches modern SSO sessions under the SHA-1 of the session name.
    cache_key = hashlib.sha1(session.encode()).hexdigest()
    cache = Path.home() / '.aws/sso/cache' / f'{cache_key}.json'
    try:
        token = json.loads(cache.read_text())['accessToken']
    except (OSError, KeyError, ValueError) as exc:
        raise RuntimeError('SSO token unavailable. Run aws sso login --sso-session '
                           f'{session} using the project AWS_CONFIG_FILE.') from exc
    # ListAccountRoles authenticates with the SSO access token. Do not resolve
    # profile credentials here: those require the role we are selecting.
    sso = sso_client(config[f'sso-session {session}']['sso_region'])
    role = select_role(sso, token, profile.get('sso_role_name'))
    profile['sso_account_id'] = ACCOUNT
    profile['sso_role_name'] = role
    with path.open('w') as handle:
        config.write(handle)
    print(f'Configured {PROFILE}: account {ACCOUNT}, role {role}')


if __name__ == '__main__':
    main()
