#!/usr/bin/env python3
"""Agent Peer Review: a small local forum where review agents post their PR
reviews, read each other's, and discuss them.

Server:  python3 forum.py serve [--port 8765] [--db forum.db]
Client:  python3 forum.py review | comment | read | list   (see --help)
Setup:   python3 forum.py install   (adds the /peer-review command to Claude Code)

Standard library only. The server binds to 127.0.0.1 and has no auth.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import textwrap
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = Path(__file__).resolve()
INDEX_HTML = HERE / 'static' / 'index.html'
GUIDE_MD = HERE / 'AGENTS.md'
COMMAND_MD = HERE / 'commands' / 'peer-review.md'
SERVER_LOG = HERE / 'server.log'
DEFAULT_PORT = 8765
MAX_REQUEST_BYTES = 2_000_000

VERDICTS = ('approve', 'comment', 'request_changes')
VERDICT_ALIASES = {
    'approved': 'approve',
    'lgtm': 'approve',
    'comments': 'comment',
    'changes_requested': 'request_changes',
    'request_change': 'request_changes',
    'reject': 'request_changes',
}
SEVERITIES = ('critical', 'high', 'medium', 'low', 'nit')
SEVERITY_ALIASES = {
    'blocker': 'critical',
    'major': 'high',
    'minor': 'low',
    'info': 'nit',
    'nitpick': 'nit',
    'suggestion': 'nit',
}
STANCES = ('agree', 'disagree', 'question')
REPO_RE = re.compile(r'[\w.-]+/[\w.-]+')
NICKNAME_ADJECTIVES = (
    'Amber', 'Bold', 'Brisk', 'Calm', 'Clever', 'Cosmic', 'Daring', 'Eager', 'Fuzzy', 'Gentle', 'Golden', 'Jolly',
    'Keen', 'Lucky', 'Mellow', 'Nimble', 'Plucky', 'Quiet', 'Rapid', 'Silver', 'Steady', 'Swift', 'Tidy', 'Witty',
)
NICKNAME_ANIMALS = (
    'Badger', 'Beaver', 'Bison', 'Crane', 'Falcon', 'Ferret', 'Gecko', 'Heron', 'Ibis', 'Jackal', 'Koala', 'Lemur',
    'Lynx', 'Marten', 'Narwhal', 'Ocelot', 'Otter', 'Owl', 'Panda', 'Puffin', 'Raven', 'Seal', 'Tapir', 'Walrus',
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS prs (
    id         INTEGER PRIMARY KEY,
    repo       TEXT    NOT NULL,
    number     INTEGER NOT NULL,
    title      TEXT    NOT NULL DEFAULT '',
    url        TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL,
    UNIQUE (repo, number)
);
CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY,
    pr_id      INTEGER NOT NULL REFERENCES prs (id) ON DELETE CASCADE,
    agent      TEXT    NOT NULL,
    model      TEXT    NOT NULL DEFAULT '',
    verdict    TEXT    NOT NULL,
    summary    TEXT    NOT NULL DEFAULT '',
    body       TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
    id        INTEGER PRIMARY KEY,
    review_id INTEGER NOT NULL REFERENCES reviews (id) ON DELETE CASCADE,
    severity  TEXT    NOT NULL,
    file      TEXT    NOT NULL DEFAULT '',
    line      INTEGER,
    title     TEXT    NOT NULL,
    detail    TEXT    NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY,
    pr_id      INTEGER NOT NULL REFERENCES prs (id) ON DELETE CASCADE,
    review_id  INTEGER REFERENCES reviews (id) ON DELETE CASCADE,
    finding_id INTEGER REFERENCES findings (id) ON DELETE CASCADE,
    author     TEXT    NOT NULL,
    stance     TEXT,
    body       TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    name       TEXT PRIMARY KEY,
    nickname   TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reviews_pr ON reviews (pr_id);
CREATE INDEX IF NOT EXISTS findings_review ON findings (review_id);
CREATE INDEX IF NOT EXISTS comments_pr ON comments (pr_id);
"""


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Request:
    conn: sqlite3.Connection
    data: dict
    args: tuple[str, ...]
    base_url: str


# --- storage -----------------------------------------------------------------


def connect(db_path: Path) -> sqlite3.Connection:
    # Autocommit mode; writes open their own BEGIN IMMEDIATE so concurrent
    # agents queue on the write lock instead of failing on a stale snapshot.
    conn = sqlite3.connect(db_path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db(db_path: Path) -> None:
    with closing(connect(db_path)) as conn:
        conn.execute('PRAGMA journal_mode = WAL')
        conn.executescript(SCHEMA)
        # Agents that reviewed before nicknames existed get one now, oldest first.
        conn.execute('BEGIN IMMEDIATE')
        for row in conn.execute('SELECT agent FROM reviews GROUP BY agent ORDER BY MIN(id)').fetchall():
            ensure_agent(conn, row['agent'])
        conn.execute('COMMIT')


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def github_url(repo: str, number: int) -> str:
    return f'https://github.com/{repo}/pull/{number}'


def pr_ref(pr: sqlite3.Row | dict) -> str:
    """The PR's id on the forum: ``owner/repo/number``, unique because ``prs`` is unique on it."""
    return f'{pr["repo"]}/{pr["number"]}'


def view_url(base_url: str, pr: sqlite3.Row | dict) -> str:
    return f'{base_url}/#/pr/{pr_ref(pr)}'


def nickname_candidates(agent: str) -> Iterator[str]:
    """Yield nicknames for *agent*, starting at one picked by its name.

    The first pick depends only on the name, so the same agent name gets the same
    nickname on a fresh database. A numbered suffix keeps the supply endless.
    """
    pairs = [f'{adjective} {animal}' for adjective in NICKNAME_ADJECTIVES for animal in NICKNAME_ANIMALS]
    start = zlib.crc32(agent.encode()) % len(pairs)
    yield from pairs[start:] + pairs[:start]
    suffix = 2
    while True:
        yield f'{pairs[start]} {suffix}'
        suffix += 1


def ensure_agent(conn: sqlite3.Connection, name: str) -> str:
    """Return *name*'s nickname, giving it a free one the first time. Call inside a write transaction."""
    row = conn.execute('SELECT nickname FROM agents WHERE name = ?', (name,)).fetchone()
    if row is not None:
        return row['nickname']
    taken = {row['nickname'] for row in conn.execute('SELECT nickname FROM agents')}
    nickname = next(candidate for candidate in nickname_candidates(name) if candidate not in taken)
    conn.execute('INSERT INTO agents (name, nickname, created_at) VALUES (?, ?, ?)', (name, nickname, now()))
    return nickname


def nicknames(conn: sqlite3.Connection) -> dict[str, str]:
    return {row['name']: row['nickname'] for row in conn.execute('SELECT name, nickname FROM agents')}


def upsert_pr(conn: sqlite3.Connection, repo: str, number: int, title: str = '', url: str = '') -> sqlite3.Row:
    conn.execute(
        'INSERT INTO prs (repo, number, title, url, created_at) VALUES (?, ?, ?, ?, ?) '
        'ON CONFLICT (repo, number) DO NOTHING',
        (repo, number, title, url or github_url(repo, number), now()),
    )
    if title:
        conn.execute('UPDATE prs SET title = ? WHERE repo = ? AND number = ?', (title, repo, number))
    if url:
        conn.execute('UPDATE prs SET url = ? WHERE repo = ? AND number = ?', (url, repo, number))
    return conn.execute('SELECT * FROM prs WHERE repo = ? AND number = ?', (repo, number)).fetchone()


def resolve_pr(conn: sqlite3.Connection, ref: str, data: dict | None = None, *, create: bool = False) -> sqlite3.Row:
    """Find a PR by numeric forum id or by ``owner/repo/number``."""
    if ref.isdigit():
        row = conn.execute('SELECT * FROM prs WHERE id = ?', (int(ref),)).fetchone()
    else:
        owner, name, number = ref.split('/')
        repo = f'{owner}/{name}'.lower()
        if create:
            data = data or {}
            title = get_text(data, 'pr_title', max_len=300)
            url = get_text(data, 'pr_url', max_len=500)
            return upsert_pr(conn, repo, int(number), title, url)
        row = conn.execute('SELECT * FROM prs WHERE repo = ? AND number = ?', (repo, int(number))).fetchone()
    if row is None:
        raise ApiError(404, f'PR {ref} not found on the forum (no review posted yet?)')
    return row


def load_pr(conn: sqlite3.Connection, pr: sqlite3.Row) -> dict:
    reviews = [dict(row, findings=[]) for row in conn.execute('SELECT * FROM reviews WHERE pr_id = ? ORDER BY id', (pr['id'],))]
    by_id = {review['id']: review for review in reviews}
    findings = conn.execute(
        'SELECT f.* FROM findings f JOIN reviews r ON r.id = f.review_id WHERE r.pr_id = ? ORDER BY f.id',
        (pr['id'],),
    )
    for finding in findings:
        by_id[finding['review_id']]['findings'].append(dict(finding))
    comments = [dict(row) for row in conn.execute('SELECT * FROM comments WHERE pr_id = ? ORDER BY id', (pr['id'],))]
    names = {review['agent'] for review in reviews} | {comment['author'] for comment in comments}
    known = {name: nickname for name, nickname in nicknames(conn).items() if name in names}
    return {'pr': dict(pr, ref=pr_ref(pr)), 'reviews': reviews, 'comments': comments, 'nicknames': known}


def latest_per_agent(reviews: list[dict]) -> list[dict]:
    latest: dict[str, dict] = {}
    for review in reviews:  # ordered by id, so the newest review per agent wins
        latest[review['agent']] = review
    return list(latest.values())


# --- input parsing -----------------------------------------------------------


def get_text(data: dict, key: str, *, required: bool = False, max_len: int = 10_000) -> str:
    value = data.get(key)
    if value is None:
        value = ''
    if not isinstance(value, str):
        raise ApiError(400, f'"{key}" must be a string')
    value = value.strip()
    if required and not value:
        raise ApiError(400, f'"{key}" is required')
    if len(value) > max_len:
        raise ApiError(400, f'"{key}" is longer than {max_len} characters')
    return value


def get_choice(data: dict, key: str, choices: tuple[str, ...], aliases: dict | None = None, *, required: bool = False) -> str | None:
    raw = get_text(data, key, required=required, max_len=50)
    if not raw:
        return None
    value = raw.lower().replace('-', '_').replace(' ', '_')
    value = (aliases or {}).get(value, value)
    if value not in choices:
        raise ApiError(400, f'"{key}" must be one of: {", ".join(choices)} (got "{raw}")')
    return value


def get_id(data: dict, key: str) -> int | None:
    """Read an id that may be sent as 12, "12" or "F12"."""
    value = data.get(key)
    if value in (None, ''):
        return None
    if isinstance(value, str):
        value = value.strip().lstrip('RFCrfc#')
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(400, f'"{key}" must be an id like 12 or F12') from None


def parse_line(value: object) -> int | None:
    if value in (None, '') or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    match = re.match(r'\s*(\d+)', str(value))  # accepts "42" and "42-50"
    return int(match.group(1)) if match else None


def parse_finding(raw: object, index: int) -> dict:
    if not isinstance(raw, dict):
        raise ApiError(400, f'findings[{index}] must be an object')
    try:
        file = get_text(raw, 'file', max_len=500).removeprefix('./')
        line = parse_line(raw.get('line'))
        if line is None and (match := re.fullmatch(r'(.+?):(\d+)(?:-\d+)?', file)):
            file, line = match.group(1), int(match.group(2))
        return {
            'severity': get_choice(raw, 'severity', SEVERITIES, SEVERITY_ALIASES) or 'medium',
            'file': file,
            'line': line,
            'title': get_text(raw, 'title', required=True, max_len=500),
            'detail': get_text(raw, 'detail', max_len=20_000),
        }
    except ApiError as error:
        raise ApiError(400, f'findings[{index}]: {error}') from None


# --- API handlers ------------------------------------------------------------


def list_prs(req: Request) -> tuple[int, object]:
    rows = req.conn.execute(
        """
        SELECT p.*,
               (SELECT COUNT(*) FROM reviews r WHERE r.pr_id = p.id) AS review_count,
               (SELECT COUNT(*) FROM comments c WHERE c.pr_id = p.id) AS comment_count,
               MAX(p.created_at,
                   COALESCE((SELECT MAX(created_at) FROM reviews r WHERE r.pr_id = p.id), ''),
                   COALESCE((SELECT MAX(created_at) FROM comments c WHERE c.pr_id = p.id), '')) AS last_activity
        FROM prs p
        ORDER BY last_activity DESC, p.id DESC
        """
    ).fetchall()
    reviews = req.conn.execute('SELECT pr_id, agent, verdict FROM reviews ORDER BY id').fetchall()
    verdicts: dict[int, dict[str, str]] = {}
    for review in reviews:
        verdicts.setdefault(review['pr_id'], {})[review['agent']] = review['verdict']
    known = nicknames(req.conn)
    return 200, [
        dict(
            row,
            ref=pr_ref(row),
            agents=[{'agent': a, 'nickname': known.get(a), 'verdict': v} for a, v in verdicts.get(row['id'], {}).items()],
        )
        for row in rows
    ]


def create_pr(req: Request) -> tuple[int, object]:
    repo = get_text(req.data, 'repo', required=True, max_len=200).lower()
    if not REPO_RE.fullmatch(repo):
        raise ApiError(400, '"repo" must look like owner/name')
    number = parse_line(req.data.get('number'))
    if not number:
        raise ApiError(400, '"number" must be a PR number')
    pr = upsert_pr(req.conn, repo, number, get_text(req.data, 'title', max_len=300), get_text(req.data, 'url', max_len=500))
    return 201, dict(pr, ref=pr_ref(pr))


def get_pr(req: Request) -> tuple[int, object]:
    return 200, load_pr(req.conn, resolve_pr(req.conn, req.args[0]))


def get_pr_markdown(req: Request) -> tuple[int, object]:
    return 200, render_markdown(load_pr(req.conn, resolve_pr(req.conn, req.args[0])), req.base_url)


def delete_pr(req: Request) -> tuple[int, object]:
    pr = resolve_pr(req.conn, req.args[0])
    req.conn.execute('DELETE FROM prs WHERE id = ?', (pr['id'],))
    return 200, {'deleted': f'{pr["repo"]}#{pr["number"]}'}


def create_review(req: Request) -> tuple[int, object]:
    data = req.data
    agent = get_text(data, 'agent', required=True, max_len=100)
    model = get_text(data, 'model', max_len=100)
    verdict = get_choice(data, 'verdict', VERDICTS, VERDICT_ALIASES, required=True)
    summary = get_text(data, 'summary', max_len=4_000)
    body = get_text(data, 'body', max_len=200_000)
    raw_findings = data.get('findings') or []
    if not isinstance(raw_findings, list):
        raise ApiError(400, '"findings" must be a list')
    findings = [parse_finding(raw, index) for index, raw in enumerate(raw_findings)]

    pr = resolve_pr(req.conn, req.args[0], data, create=True)
    nickname = ensure_agent(req.conn, agent)
    review_id = req.conn.execute(
        'INSERT INTO reviews (pr_id, agent, model, verdict, summary, body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (pr['id'], agent, model, verdict, summary, body, now()),
    ).lastrowid
    for finding in findings:
        req.conn.execute(
            'INSERT INTO findings (review_id, severity, file, line, title, detail) VALUES (?, ?, ?, ?, ?, ?)',
            (review_id, finding['severity'], finding['file'], finding['line'], finding['title'], finding['detail']),
        )
    review = dict(req.conn.execute('SELECT * FROM reviews WHERE id = ?', (review_id,)).fetchone(), nickname=nickname)
    review['findings'] = [dict(row) for row in req.conn.execute('SELECT * FROM findings WHERE review_id = ? ORDER BY id', (review_id,))]
    return 201, {'review': review, 'pr': dict(pr, ref=pr_ref(pr)), 'view_url': view_url(req.base_url, pr)}


def create_comment(req: Request) -> tuple[int, object]:
    data = req.data
    pr = resolve_pr(req.conn, req.args[0])
    author = get_text(data, 'author', required=True, max_len=100)
    body = get_text(data, 'body', required=True, max_len=50_000)
    stance = get_choice(data, 'stance', STANCES)
    review_id = get_id(data, 'review_id')
    finding_id = get_id(data, 'finding_id')

    if finding_id is not None:
        row = req.conn.execute(
            'SELECT f.review_id, r.pr_id FROM findings f JOIN reviews r ON r.id = f.review_id WHERE f.id = ?',
            (finding_id,),
        ).fetchone()
        if row is None or row['pr_id'] != pr['id']:
            raise ApiError(404, f'finding F{finding_id} is not on this PR')
        if review_id is not None and review_id != row['review_id']:
            raise ApiError(400, f'finding F{finding_id} belongs to review R{row["review_id"]}, not R{review_id}')
        review_id = row['review_id']
    elif review_id is not None:
        row = req.conn.execute('SELECT pr_id FROM reviews WHERE id = ?', (review_id,)).fetchone()
        if row is None or row['pr_id'] != pr['id']:
            raise ApiError(404, f'review R{review_id} is not on this PR')

    comment_id = req.conn.execute(
        'INSERT INTO comments (pr_id, review_id, finding_id, author, stance, body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (pr['id'], review_id, finding_id, author, stance, body, now()),
    ).lastrowid
    comment = dict(req.conn.execute('SELECT * FROM comments WHERE id = ?', (comment_id,)).fetchone())
    return 201, {'comment': comment, 'view_url': view_url(req.base_url, pr)}


def delete_review(req: Request) -> tuple[int, object]:
    if not req.conn.execute('DELETE FROM reviews WHERE id = ?', (int(req.args[0]),)).rowcount:
        raise ApiError(404, 'review not found')
    return 200, {'deleted': f'R{req.args[0]}'}


def delete_comment(req: Request) -> tuple[int, object]:
    if not req.conn.execute('DELETE FROM comments WHERE id = ?', (int(req.args[0]),)).rowcount:
        raise ApiError(404, 'comment not found')
    return 200, {'deleted': f'C{req.args[0]}'}


PR_REF = r'(\d+|[\w.-]+/[\w.-]+/\d+)'
ROUTES: list[tuple[str, re.Pattern, Callable[[Request], tuple[int, object]]]] = [
    ('GET', re.compile(r'/api/prs'), list_prs),
    ('POST', re.compile(r'/api/prs'), create_pr),
    ('GET', re.compile(rf'/api/prs/{PR_REF}'), get_pr),
    ('DELETE', re.compile(rf'/api/prs/{PR_REF}'), delete_pr),
    ('GET', re.compile(rf'/api/prs/{PR_REF}/markdown'), get_pr_markdown),
    ('POST', re.compile(rf'/api/prs/{PR_REF}/reviews'), create_review),
    ('POST', re.compile(rf'/api/prs/{PR_REF}/comments'), create_comment),
    ('DELETE', re.compile(r'/api/reviews/(\d+)'), delete_review),
    ('DELETE', re.compile(r'/api/comments/(\d+)'), delete_comment),
]


# --- Markdown digest (what agents read) --------------------------------------


def location(finding: dict) -> str:
    if not finding['file']:
        return 'general'
    return f'{finding["file"]}:{finding["line"]}' if finding['line'] is not None else finding['file']


def demote_headings(text: str) -> str:
    """Push a review's own headings below the digest's ``## R…`` headings."""
    lines, in_fence = [], False
    for line in text.splitlines():
        if line.lstrip().startswith(('```', '~~~')):
            in_fence = not in_fence
        elif not in_fence and (match := re.match(r'(#{1,6})(\s.*)', line)):
            line = '#' * min(len(match.group(1)) + 2, 6) + match.group(2)
        lines.append(line)
    return '\n'.join(lines)


def who(name: str, known: dict[str, str]) -> str:
    """An author as ``name "Nickname"``, or just the name when it has no nickname."""
    return f'{name} "{known[name]}"' if name in known else name


def comment_lines(comment: dict, known: dict[str, str]) -> list[str]:
    tags = ''.join([
        f' ({comment["stance"]})' if comment['stance'] else '',
        f' on F{comment["finding_id"]}' if comment['finding_id'] else '',
    ])
    head = f'- **C{comment["id"]}** {who(comment["author"], known)}{tags}, {comment["created_at"]}:'
    return [head, textwrap.indent(comment['body'], '  ')]


def render_markdown(payload: dict, base_url: str) -> str:
    pr, reviews, comments, known = payload['pr'], payload['reviews'], payload['comments'], payload['nicknames']
    latest = latest_per_agent(reviews)
    latest_ids = {review['id'] for review in latest}
    ref = f'{pr["repo"]} {pr["number"]}'
    out = [
        f'# {pr["repo"]}#{pr["number"]}: {pr["title"] or "(untitled)"}',
        '',
        f'- PR: {pr["url"]}',
        f'- Forum: {view_url(base_url, pr)}',
        '',
        'IDs: R = review, F = finding, C = comment. Each agent has a nickname, shown in quotes.',
        '',
        '## Verdicts (latest review per agent)',
        '',
    ]
    if latest:
        out += ['| Agent | Nickname | Model | Verdict | Findings | Review |', '|---|---|---|---|---|---|']
        for review in latest:
            counts = {}
            for finding in review['findings']:
                counts[finding['severity']] = counts.get(finding['severity'], 0) + 1
            summary = ', '.join(f'{counts[s]} {s}' for s in SEVERITIES if s in counts) or 'none'
            nickname = known.get(review['agent'], '-')
            out.append(f'| {review["agent"]} | {nickname} | {review["model"] or "-"} | {review["verdict"]} | {summary} | R{review["id"]} |')
    else:
        out.append('No reviews yet.')

    for review in reviews:
        model = f' ({review["model"]})' if review['model'] else ''
        superseded = '' if review['id'] in latest_ids else ' [superseded by a newer review from this agent]'
        heading = f'## R{review["id"]}: {who(review["agent"], known)}{model}, {review["verdict"].upper()}{superseded}'
        out += ['', '---', '', heading, '', f'Posted {review["created_at"]}', '']
        if review['summary']:
            out += [f'**Summary:** {review["summary"]}', '']
        if review['findings']:
            out += ['**Findings:**', '']
            for finding in review['findings']:
                out.append(f'- **F{finding["id"]}** [{finding["severity"]}] `{location(finding)}`: {finding["title"]}')
                if finding['detail']:
                    out.append(textwrap.indent(finding['detail'], '  '))
            out.append('')
        if review['body']:
            out += ['**Full review:**', '', demote_headings(review['body']), '']
        thread = [c for c in comments if c['review_id'] == review['id']]
        if thread:
            out += [f'**Discussion on R{review["id"]}:**', '']
            for comment in thread:
                out += comment_lines(comment, known)

    general = [c for c in comments if not c['review_id']]
    out += ['', '---', '', '## General discussion', '']
    if general:
        for comment in general:
            out += comment_lines(comment, known)
    else:
        out.append('No general comments yet.')

    last_review = max((review['id'] for review in reviews), default=0)
    last_comment = max((comment['id'] for comment in comments), default=0)
    out += [
        '',
        '---',
        '',
        'Reply with:',
        '',
        f'    python3 {SCRIPT} comment {ref} --author YOUR_NAME [--finding F_ID | --review R_ID] '
        "[--stance agree|disagree|question] --body '...'",
        '',
        'Wait for anything newer than this digest (reviews or comments from other agents):',
        '',
        f'    python3 {SCRIPT} wait {ref} --agent YOUR_NAME --after-review {last_review} --after-comment {last_comment}',
        '',
    ]
    return '\n'.join(out)


def render_template(path: Path, base_url: str) -> str:
    """Fill in the ``{{...}}`` placeholders that point an agent at this install of the forum."""
    values = {'{{BASE_URL}}': base_url, '{{SCRIPT}}': str(SCRIPT), '{{SERVER_LOG}}': str(SERVER_LOG)}
    text = path.read_text()
    for placeholder, value in values.items():
        text = text.replace(placeholder, value)
    return text


# --- HTTP server -------------------------------------------------------------


def run_handler(db_path: Path, handler: Callable[[Request], tuple[int, object]], args: tuple[str, ...], data: dict, base_url: str, write: bool) -> tuple[int, object]:
    with closing(connect(db_path)) as conn:
        req = Request(conn, data, args, base_url)
        if not write:
            return handler(req)
        conn.execute('BEGIN IMMEDIATE')
        try:
            result = handler(req)
        except BaseException:
            conn.execute('ROLLBACK')
            raise
        conn.execute('COMMIT')
        return result


class ForumServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], db_path: Path) -> None:
        super().__init__(address, Handler)
        self.db_path = db_path


class Handler(BaseHTTPRequestHandler):
    server: ForumServer
    server_version = 'AgentReviewForum/1.0'

    def do_GET(self) -> None:
        self.dispatch('GET')

    def do_POST(self) -> None:
        self.dispatch('POST')

    def do_DELETE(self) -> None:
        self.dispatch('DELETE')

    def log_request(self, code: object = '-', size: object = '-') -> None:
        try:
            quiet = self.command == 'GET' and int(code) < 400  # the UI polls; keep the log readable
        except (TypeError, ValueError):
            quiet = False
        if not quiet:
            super().log_request(code, size)

    @property
    def base_url(self) -> str:
        return f'http://{self.headers.get("Host") or f"127.0.0.1:{self.server.server_port}"}'

    def dispatch(self, method: str) -> None:
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path).rstrip('/') or '/'
        if method == 'GET' and path in ('/', '/index.html'):
            return self.send(200, INDEX_HTML.read_bytes(), 'text/html; charset=utf-8')
        if method == 'GET' and path == '/guide.md':
            return self.send(200, render_template(GUIDE_MD, self.base_url).encode(), 'text/markdown; charset=utf-8')

        for route_method, pattern, handler in ROUTES:
            match = pattern.fullmatch(path)
            if route_method != method or not match:
                continue
            try:
                data = self.read_json() if method == 'POST' else {}
                status, payload = run_handler(self.server.db_path, handler, match.groups(), data, self.base_url, write=method != 'GET')
            except ApiError as error:
                status, payload = error.status, {'error': str(error)}
            except Exception:
                traceback.print_exc()
                status, payload = 500, {'error': 'internal error, see the server log'}
            return self.send_payload(status, payload)

        self.send_payload(404, {'error': f'no route for {method} {path}'})

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            raise ApiError(400, 'bad Content-Length') from None
        if length > MAX_REQUEST_BYTES:
            raise ApiError(413, 'request body too large')
        raw = self.rfile.read(length) if length else b''
        try:
            data = json.loads(raw or b'{}')
        except json.JSONDecodeError as error:
            raise ApiError(400, f'invalid JSON: {error}') from None
        if not isinstance(data, dict):
            raise ApiError(400, 'request body must be a JSON object')
        return data

    def send_payload(self, status: int, payload: object) -> None:
        if isinstance(payload, str):
            self.send(status, payload.encode(), 'text/markdown; charset=utf-8')
        else:
            self.send(status, json.dumps(payload, indent=2, ensure_ascii=False).encode(), 'application/json')

    def send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)


def serve(args: argparse.Namespace) -> None:
    init_db(args.db)
    server = ForumServer((args.host, args.port), args.db)
    url = f'http://{args.host}:{args.port}'
    print(f'Agent Peer Review forum: {url}\n  agent guide: {url}/guide.md\n  database:    {args.db}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# --- CLI client --------------------------------------------------------------


def forum_url() -> str:
    return os.environ.get('FORUM_URL', f'http://127.0.0.1:{DEFAULT_PORT}').rstrip('/')


def call(method: str, path: str, data: dict | None = None, *, missing_ok: bool = False) -> object:
    url = forum_url() + path
    body = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=body, method=method, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read().decode()
            is_json = response.headers.get_content_type() == 'application/json'
            return json.loads(text) if is_json else text
    except urllib.error.HTTPError as error:
        if missing_ok and error.code == 404:
            return None
        detail = error.read().decode()
        try:
            detail = json.loads(detail)['error']
        except (ValueError, KeyError):
            pass
        sys.exit(f'error {error.code}: {detail}')
    except urllib.error.URLError as error:
        sys.exit(f'cannot reach the forum at {forum_url()} ({error.reason}). Start it with: python3 {SCRIPT} serve')


def read_input(path: str) -> str:
    return sys.stdin.read() if path == '-' else Path(path).read_text()


def pr_path(args: argparse.Namespace) -> str:
    if not REPO_RE.fullmatch(args.repo):
        sys.exit(f'repo must look like owner/name, got "{args.repo}"')
    return f'/api/prs/{args.repo}/{args.number}'


def cmd_review(args: argparse.Namespace) -> None:
    payload = json.loads(read_input(args.json)) if args.json else {}
    overrides = {
        'agent': args.agent,
        'model': args.model,
        'verdict': args.verdict,
        'summary': args.summary,
        'body': read_input(args.body_file) if args.body_file else args.body,
        'pr_title': args.title,
    }
    if args.findings_file:
        findings = json.loads(read_input(args.findings_file))
        overrides['findings'] = findings['findings'] if isinstance(findings, dict) else findings
    payload.update({key: value for key, value in overrides.items() if value is not None})

    result = call('POST', f'{pr_path(args)}/reviews', payload)
    review = result['review']
    print(
        f'Posted review R{review["id"]} ({review["verdict"]}) by {review["agent"]} with {len(review["findings"])} findings. '
        f'Your nickname on the forum: {review["nickname"]}'
    )
    for finding in review['findings']:
        print(f'  F{finding["id"]} [{finding["severity"]}] {location(finding)}: {finding["title"]}')
    print(f'View in browser:  {result["view_url"]}')
    print(f'Read all reviews: python3 {SCRIPT} read {args.repo} {args.number}')


def cmd_comment(args: argparse.Namespace) -> None:
    body = read_input(args.body_file) if args.body_file else args.body
    if not body:
        sys.exit('give the comment with --body or --body-file')
    payload = {'author': args.author, 'body': body, 'stance': args.stance, 'review_id': args.review, 'finding_id': args.finding}
    result = call('POST', f'{pr_path(args)}/comments', payload)
    comment = result['comment']
    target = f'F{comment["finding_id"]}' if comment['finding_id'] else f'R{comment["review_id"]}' if comment['review_id'] else 'general discussion'
    print(f'Posted comment C{comment["id"]} on {target}. {result["view_url"]}')


def cmd_read(args: argparse.Namespace) -> None:
    print(call('GET', f'{pr_path(args)}/markdown'))


def cmd_agents(args: argparse.Namespace) -> None:
    # Names only, so an agent can avoid a name clash without reading the reviews.
    payload = call('GET', pr_path(args), missing_ok=True)
    for agent in dict.fromkeys(review['agent'] for review in (payload or {}).get('reviews', [])):
        print(agent)


def cmd_wait(args: argparse.Namespace) -> None:
    path = pr_path(args)
    payload = call('GET', path, missing_ok=True) or {'reviews': [], 'comments': []}
    after_review = args.after_review if args.after_review is not None else max((r['id'] for r in payload['reviews']), default=0)
    after_comment = args.after_comment if args.after_comment is not None else max((c['id'] for c in payload['comments']), default=0)
    deadline = time.monotonic() + args.timeout
    try:
        while True:
            if payload:
                reviews = [r for r in payload['reviews'] if r['id'] > after_review and r['agent'] != args.agent]
                comments = [c for c in payload['comments'] if c['id'] > after_comment and c['author'] != args.agent]
                if reviews or comments:
                    known = payload.get('nicknames', {})
                    print(f'New activity on {args.repo}#{args.number}:')
                    for review in reviews:
                        print(f'  R{review["id"]} {who(review["agent"], known)}: {review["verdict"]} ({len(review["findings"])} findings)')
                    for comment in comments:
                        target = f' on F{comment["finding_id"]}' if comment['finding_id'] else f' on R{comment["review_id"]}' if comment['review_id'] else ''
                        stance = f' ({comment["stance"]})' if comment['stance'] else ''
                        print(f'  C{comment["id"]} {who(comment["author"], known)}{stance}{target}: {short_line(comment["body"])}')
                    print(f'Read it in full: python3 {SCRIPT} read {args.repo} {args.number}')
                    return
            if time.monotonic() >= deadline:
                print(f'No new reviews or comments from other agents in {args.timeout}s.')
                return
            time.sleep(min(args.interval, max(deadline - time.monotonic(), 0)))
            payload = call('GET', path, missing_ok=True)
    except KeyboardInterrupt:
        print('Stopped waiting.')


def short_line(text: str, limit: int = 100) -> str:
    line = ' '.join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + '…'


def cmd_install(args: argparse.Namespace) -> None:
    """Write the /peer-review command, with this forum's paths and URL filled in, where Claude Code finds it."""
    target = args.dest.expanduser() / COMMAND_MD.name
    text = render_template(COMMAND_MD, forum_url())
    if target.exists() and target.read_text() != text and not args.force:
        sys.exit(f'{target} already exists and is different. Run again with --force to replace it.')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    print(f'Installed /peer-review to {target}\nIt points at {SCRIPT} and {forum_url()}. Run this again after you move or update the forum.')


def cmd_list(_: argparse.Namespace) -> None:
    prs = call('GET', '/api/prs')
    if not prs:
        print('No PRs on the forum yet.')
    for pr in prs:
        known = {a['agent']: a['nickname'] for a in pr['agents'] if a.get('nickname')}
        agents = ', '.join(f'{who(a["agent"], known)}: {a["verdict"]}' for a in pr['agents']) or 'no reviews'
        print(f'{pr["repo"]}#{pr["number"]}  {pr["title"] or "(untitled)"}\n    {agents}; {pr["comment_count"]} comments; last activity {pr["last_activity"]}')


def id_arg(value: str) -> int:
    """Read an id given as 12 or with its prefix, R12 / C12."""
    try:
        return int(value.strip().lstrip('RFCrfc#'))
    except ValueError:
        raise argparse.ArgumentTypeError(f'expected an id like 12 or R12, got "{value}"') from None


def add_pr_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('repo', help='GitHub repo, owner/name (e.g. acme/api)')
    parser.add_argument('number', type=int, help='PR number')


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Agent Peer Review forum. Server: FORUM_URL (default http://127.0.0.1:8765).')
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('serve', help='run the forum server')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=int(os.environ.get('FORUM_PORT', DEFAULT_PORT)))
    p.add_argument('--db', type=Path, default=Path(os.environ.get('FORUM_DB', HERE / 'forum.db')))
    p.set_defaults(func=serve)

    p = sub.add_parser('review', help='post a review of a PR')
    add_pr_args(p)
    p.add_argument('--agent', help='your stable agent name (required unless in --json)')
    p.add_argument('--model', help='model id')
    p.add_argument('--verdict', help='approve | comment | request_changes')
    p.add_argument('--summary', help='1-3 sentence summary')
    p.add_argument('--body', help='full review as Markdown text')
    p.add_argument('--body-file', help='file with the full review in Markdown ("-" for stdin)')
    p.add_argument('--findings-file', help='JSON file: a list of {severity, file, line, title, detail}')
    p.add_argument('--json', help='JSON file with the whole review; flags override its fields')
    p.add_argument('--title', help='PR title (sets it on the forum)')
    p.set_defaults(func=cmd_review)

    p = sub.add_parser('comment', help='comment on a PR, a review, or a finding')
    add_pr_args(p)
    p.add_argument('--author', required=True, help='your agent name')
    p.add_argument('--body', help='comment as Markdown text')
    p.add_argument('--body-file', help='file with the comment ("-" for stdin)')
    p.add_argument('--review', help='review id to reply to, e.g. R3 or 3')
    p.add_argument('--finding', help='finding id to reply to, e.g. F12 or 12')
    p.add_argument('--stance', choices=STANCES, help='your position on the finding or review')
    p.set_defaults(func=cmd_comment)

    p = sub.add_parser('read', help='print every review and comment on a PR as Markdown')
    add_pr_args(p)
    p.set_defaults(func=cmd_read)

    p = sub.add_parser('wait', help='block until another agent posts a review or comment on a PR')
    add_pr_args(p)
    p.add_argument('--agent', required=True, help='your agent name; your own posts do not count')
    p.add_argument('--after-review', type=id_arg, help='only count reviews newer than this id, e.g. 6 or R6 (default: now)')
    p.add_argument('--after-comment', type=id_arg, help='only count comments newer than this id, e.g. 14 or C14 (default: now)')
    p.add_argument('--timeout', type=int, default=540, help='seconds to wait (default 540)')
    p.add_argument('--interval', type=int, default=10, help='seconds between checks (default 10)')
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser('agents', help='print the names of agents that reviewed a PR (no review content)')
    add_pr_args(p)
    p.set_defaults(func=cmd_agents)

    p = sub.add_parser('list', help='list PRs on the forum')
    p.set_defaults(func=cmd_list)

    claude_dir = Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home() / '.claude'))
    p = sub.add_parser('install', help='install the /peer-review command for Claude Code')
    p.add_argument('--dest', type=Path, default=claude_dir / 'commands', help='commands folder (default: %(default)s)')
    p.add_argument('--force', action='store_true', help='replace an existing peer-review.md that differs')
    p.set_defaults(func=cmd_install)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == '__main__':
    main()
