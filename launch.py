#!/usr/bin/env python3
"""Run several /peer-review agents on one PR, one after another, each one a headless ``claude -p`` run.

    python3 launch.py acme/api#123 \\
        --agent opus "security and tenant isolation" \\
        --agent sonnet "data: migrations, backfills, slow queries" \\
        --agent fable

Each ``--agent`` is a model (an alias such as opus, sonnet, fable or haiku, or a full model id) and an optional lens:
what that agent should focus on. With no ``--agent``, it runs ``DEFAULT_AGENTS``: three opus agents, with the lenses
"check security", "review and check for bugs" and "review the code and test it". An agent starts only when the one
before it has finished. It writes its own review first, then reads every review and comment posted before it and
answers them. Each agent's final report goes to ``runs/<repo>-<pr>-<time>/<n>-<agent>.log``.

Environment:
    FORUM_OWNER      owner for a short target like ``api#123`` (default: the current directory's repo owner)
    FORUM_REPOS_DIR  folder of local clones; agents run in ``$FORUM_REPOS_DIR/<repo>`` when it exists
    FORUM_URL        the forum (default http://127.0.0.1:8765)

Standard library only. Needs ``claude`` and ``gh`` on PATH, and the /peer-review command (``python3 forum.py install``).
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import forum

RUNS = forum.HERE / 'runs'
URL_RE = re.compile(r'https?://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)\S*')
TARGET_RE = re.compile(r'(?:([\w.-]+(?:/[\w.-]+)?)#)?#?(\d+)')
NAME_STOPWORDS = {'a', 'an', 'and', 'check', 'code', 'for', 'in', 'it', 'of', 'on', 'or', 'review', 'the', 'to', 'with'}
# (model, lens) for each agent when no --agent is given, in run order.
DEFAULT_AGENTS = (
    ('opus', 'check security'),
    ('opus', 'review and check for bugs'),
    ('opus', 'review the code and test it'),
)


@dataclass
class Agent:
    model: str
    lens: str
    name: str = ''


def current_repo(query: str) -> str:
    """Ask gh about the GitHub repo in the current directory, e.g. ``.nameWithOwner``. Empty if there is none."""
    field = query.lstrip('.').split('.')[0]
    result = subprocess.run(['gh', 'repo', 'view', '--json', field, '-q', query], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ''


def parse_target(target: str) -> tuple[str, int]:
    """Turn ``123``, ``#123``, ``api#123``, ``owner/api#123`` or a PR URL into ``(owner/repo, number)``."""
    if match := URL_RE.fullmatch(target):
        return match.group(1).lower(), int(match.group(2))
    match = TARGET_RE.fullmatch(target)
    if not match:
        sys.exit(f'cannot read the PR target "{target}". Use 123, api#123, owner/api#123 or a PR URL.')
    repo, number = match.group(1), int(match.group(2))
    if repo is None:
        repo = current_repo('.nameWithOwner')
        if not repo:
            sys.exit(f'no repo in "{target}" and the current directory is not a GitHub repo. Use owner/repo#{number}.')
    elif '/' not in repo:
        owner = os.environ.get('FORUM_OWNER') or current_repo('.owner.login')
        if not owner:
            sys.exit(f'no owner for "{target}". Use owner/{repo}#{number}, or set FORUM_OWNER.')
        repo = f'{owner}/{repo}'
    return repo.lower(), number


def forum_up() -> bool:
    try:
        with urllib.request.urlopen(f'{forum.forum_url()}/api/prs', timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def start_forum() -> None:
    """Start the forum server so it outlives this script, the same way /peer-review does."""
    with forum.SERVER_LOG.open('a') as log:
        subprocess.Popen(
            [sys.executable, str(forum.SCRIPT), 'serve'],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    for _ in range(20):
        if forum_up():
            return
        time.sleep(0.25)
    sys.exit(f'the forum did not start. Run it yourself: python3 {forum.SCRIPT} serve')


def agents_on_forum(repo: str, number: int) -> set[str]:
    payload = forum.call('GET', f'/api/prs/{repo}/{number}', missing_ok=True) or {}
    return {review['agent'] for review in payload.get('reviews', [])}


def base_name(agent: Agent) -> str:
    """``opus`` with no lens, ``opus-security-tenant`` with the lens "security and tenant isolation"."""
    model = re.sub(r'[^\w.-]+', '-', agent.model).strip('-')
    words = [word for word in re.findall(r'[a-z0-9]+', agent.lens.lower()) if word not in NAME_STOPWORDS]
    return '-'.join([model, *words[:2]])


def assign_names(agents: list[Agent], taken: set[str]) -> None:
    """Give every agent a name no one has used on this PR, so no agent has to ask about a clash."""
    taken = set(taken)
    for agent in agents:
        name, suffix = base_name(agent), 2
        while name in taken:
            name, suffix = f'{base_name(agent)}-{suffix}', suffix + 1
        agent.name = name
        taken.add(name)


def claude_command(agent: Agent, repo: str, number: int, args: argparse.Namespace) -> list[str]:
    # --no-wait: the agents before this one are done and the ones after have not started, so nobody would answer.
    prompt = f'/peer-review {repo}#{number} --as {agent.name} --no-wait'
    if agent.lens:
        prompt += ' --lens "{}"'.format(agent.lens.replace('"', "'"))
    command = ['claude', '-p', prompt, '--model', agent.model, '--name', f'peer-review {agent.name}']
    # Anything the command's allowed-tools does not cover is denied instead of waiting on a prompt nobody sees.
    command += ['--permission-prompts', 'none']
    if args.effort:
        command += ['--effort', args.effort]
    if args.budget:
        command += ['--max-budget-usd', str(args.budget)]
    return command


def duration(seconds: float) -> str:
    return f'{int(seconds // 60)}m{int(seconds % 60):02d}s'


def label(agent: Agent) -> str:
    return f'{agent.name} ({agent.lens})' if agent.lens else agent.name


def run_in_turn(agents: list[Agent], repo: str, number: int, cwd: Path, run_dir: Path, args: argparse.Namespace) -> list[Agent]:
    """Run the agents one at a time and print each one's report as it finishes. Returns the ones that failed."""
    run_dir.mkdir(parents=True, exist_ok=True)
    failed = []
    for index, agent in enumerate(agents, start=1):
        log_path = run_dir / f'{index}-{agent.name}.log'
        print(f'\n[{index}/{len(agents)}] {label(agent)} on {agent.model}. Log: {log_path}', flush=True)
        started = time.monotonic()
        with log_path.open('w') as log:
            code = subprocess.run(
                claude_command(agent, repo, number, args),
                cwd=cwd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            ).returncode
        status = 'done' if code == 0 else f'FAILED (exit {code})'
        print(f'[{index}/{len(agents)}] {agent.name}: {status} after {duration(time.monotonic() - started)}\n', flush=True)
        print(log_path.read_text().strip() or '(no output)', flush=True)
        if code != 0:
            failed.append(agent)
    return failed


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Run several /peer-review agents on one PR, one after another, each with its own model and lens.',
        epilog='Example: python3 launch.py acme/api#123 --agent opus "security and tenant isolation" --agent sonnet "data and migrations" --agent fable',
    )
    parser.add_argument('target', help='PR: 123, api#123, owner/api#123 or a GitHub PR URL')
    default_agents = '; '.join(f'{model} "{lens}"' for model, lens in DEFAULT_AGENTS)
    parser.add_argument(
        '--agent', action='append', nargs='+', metavar=('MODEL', 'LENS'),
        help='one reviewer: a model, then an optional lens (what to focus on). Repeat for each agent, in run order. '
        f'Without it: {default_agents}.',
    )
    parser.add_argument('--effort', choices=('low', 'medium', 'high', 'xhigh', 'max'), help='effort level for every agent')
    parser.add_argument('--budget', type=float, help='max dollars each agent may spend (claude --max-budget-usd)')
    parser.add_argument('--dry-run', action='store_true', help='print the claude commands and stop')
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    repo, number = parse_target(args.target)
    # No argparse default: with action='append' it would add the given agents to the defaults, not replace them.
    agent_specs = args.agent or [[model, lens] for model, lens in DEFAULT_AGENTS]
    agents = [Agent(model=values[0], lens=' '.join(values[1:]).strip()) for values in agent_specs]

    if not forum_up() and not args.dry_run:
        print('Starting the forum...', flush=True)
        start_forum()
    assign_names(agents, agents_on_forum(repo, number) if forum_up() else set())

    name = repo.split('/')[1]
    repos_dir = os.environ.get('FORUM_REPOS_DIR')
    clone = Path(repos_dir).expanduser() / name if repos_dir else None
    # Run inside the local clone when there is one, so each agent loads that repo's CLAUDE.md.
    cwd = clone if clone and clone.is_dir() else Path.cwd()
    run_dir = RUNS / f'{name}-{number}-{datetime.now():%Y%m%d-%H%M%S}'
    if args.dry_run:
        print(f'# in {cwd}, one after another')
        for agent in agents:
            print(shlex.join(claude_command(agent, repo, number, args)))
        return

    print(f'Reviewing {repo}#{number} with {len(agents)} agents, one after another. Watch live: {forum.forum_url()}/#/pr/{repo}/{number}', flush=True)
    try:
        failed = run_in_turn(agents, repo, number, cwd, run_dir, args)
    except KeyboardInterrupt:
        sys.exit('\nStopped. The agent that was running was stopped too.')

    print(f'\nForum: {forum.forum_url()}/#/pr/{repo}/{number}\nLogs:  {run_dir}')
    if failed:
        sys.exit(f'{len(failed)} agent(s) failed: {", ".join(agent.name for agent in failed)}')


if __name__ == '__main__':
    main()
