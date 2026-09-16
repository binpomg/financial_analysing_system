"""Fail closed on private files, token patterns and unreviewed URL hosts.

This is an additional publication check, not a proof that arbitrary text is safe.
Reports contain locations/rule names only, never matched values. Real credentials
and private routing can also be checked in memory via repeated --deny-env names.
"""
import argparse
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, quote

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_HOSTS = {"github.com", "www.cninfo.com.cn", "static.cninfo.com.cn", "www.w3.org", "127.0.0.1", "localhost"}
URL = re.compile(r"https?://[^\s<>\"'`]+")
TOKEN = re.compile(r"(?:\bsk-[A-Za-z0-9_-]{16,}|\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}|\bAKIA[A-Z0-9]{16}\b|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")
WINDOWS_HOME = re.compile(r"[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]", re.I)
FORBIDDEN_PARTS = {"runtime", ".venv", "venv", "__pycache__", ".codex", ".ssh", "private", "data", "outputs", "logs"}
FORBIDDEN_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".pyc", ".log", ".sqlite", ".db", ".pdf", ".xlsx", ".xls", ".docx", ".pptx"}


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], stderr=subprocess.PIPE)


def forbidden_path(name):
    p = PurePosixPath(name.replace("\\", "/"))
    return (any(part in FORBIDDEN_PARTS or part.startswith('.venv') for part in p.parts)
            or p.name in {"config.local.json", "config.toml", ".env"}
            or (p.name.startswith('.env.') and p.name != '.env.example')
            or p.suffix.lower() in FORBIDDEN_SUFFIXES)


def inspect_bytes(data, forbidden_values=()):
    problems = set()
    text = data.decode('utf-8', errors='replace')
    if b'\x00' in data:
        problems.add('unexpected_binary')
    if TOKEN.search(text):
        problems.add('credential_pattern')
    if WINDOWS_HOME.search(text):
        problems.add('personal_machine_path')
    for value in forbidden_values:
        if value and any(v in text for v in {value, quote(value, safe=''), json.dumps(value)[1:-1]}):
            problems.add('private_value')
    for candidate in URL.findall(text):
        try:
            parsed = urlsplit(candidate)
            host = parsed.hostname or ''
            if parsed.username or parsed.password:
                problems.add('url_credentials')
            # RFC-reserved domains occur in offline test fixtures only.
            reserved = (host in {'example.com', 'example.org', 'example.net'}
                        or host.endswith(('.example', '.invalid', '.test')))
            if host not in PUBLIC_HOSTS and not reserved:
                problems.add('unreviewed_url_host')
        except ValueError:
            problems.add('invalid_url_literal')
    return sorted(problems)


def inventory(staged=False, history=False):
    if history:
        seen = set()
        for line in git('rev-list', '--objects', '--all').decode().splitlines():
            oid, _, name = line.partition(' ')
            if oid in seen or git('cat-file', '-t', oid).strip() != b'blob':
                continue
            seen.add(oid)
            yield name or ('object-' + oid[:12]), git('cat-file', 'blob', oid)
        return
    names = git('ls-files', '-z', '--cached') if staged else git('ls-files', '-z', '--cached', '--others', '--exclude-standard')
    for raw in sorted(set(names.split(b'\0')) - {b''}):
        name = raw.decode('utf-8')
        if staged:
            yield name, git('show', ':' + name)
        elif (ROOT / name).is_file():
            yield name, (ROOT / name).read_bytes()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--staged', action='store_true')
    p.add_argument('--all-history', action='store_true')
    p.add_argument('--deny-env', action='append', default=[], metavar='ENV_NAME')
    args = p.parse_args()
    if any(not os.environ.get(name) for name in args.deny_env):
        print(json.dumps({'success': False, 'error': 'A requested private-value environment variable is missing'}))
        return 1
    values = [os.environ[name] for name in args.deny_env]
    findings = []
    count = 0
    for name, data in inventory(args.staged, args.all_history):
        count += 1
        rules = inspect_bytes(data, values)
        if forbidden_path(name):
            rules.append('private_or_generated_path')
        findings.extend({'path': name, 'rule': rule} for rule in rules)
    print(json.dumps({'success': not findings, 'files_or_blobs_checked': count, 'findings': findings}, ensure_ascii=True))
    return 1 if findings else 0


if __name__ == '__main__':
    raise SystemExit(main())
